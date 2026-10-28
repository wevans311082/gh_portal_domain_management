from datetime import timedelta
from importlib import import_module
from io import StringIO
from unittest.mock import patch
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.management import call_command, CommandError
from django.db import connection, DatabaseError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.models import Domain, DomainTransferAttempt, DomainTransfer
from apps.domains.registrar_review import registrar_review_required
from tests.test_transfer_submission import transfer, provider, submit, drive

pytestmark = pytest.mark.django_db


def seed():
    migration = import_module("apps.domains.migrations.0018_transfer_submission_guard")
    migration.quarantine_historical_transfers(apps, SimpleNamespace(connection=connection))


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_superuser(email="transfer-owner@example.test", password="testpass")


def review(attempt, owner, action, **kwargs):
    call_command("reconcile_transfer", attempt_id=attempt.pk, owner_id=owner.pk, action=action,
                 provider_reference="CASE-TRANSFER", reason="Provider support reviewed the original transfer request", stdout=StringIO(), **kwargs)


def payload(transfer, **changes):
    value = {"orderid": "501", "domainname": transfer.domain_name, "customerid": "999", "currentstatus": "InActive",
             "registrantcontactid": "201", "admincontactid": "201", "techcontactid": "201", "billingcontactid": "201",
             "noOfNameServers": 0, "creationtime": "0", "endtime": "0", "domsecret": "DO-NOT-PERSIST"}
    value.update(changes)
    return value


@pytest.mark.parametrize("status", ["pending_payment", "paid", "processing", "failed", "cancelled", "completed"])
def test_historical_states_are_not_evidence_of_safe_resubmission(transfer, status):
    transfer.status = status
    transfer.save()
    seed()
    seed()
    attempt = DomainTransferAttempt.objects.get()
    assert attempt.legacy_review_required and attempt.status == "unknown"
    assert attempt.subject == {"legacy_original_status": status}


def test_consistent_completed_asset_is_preserved_but_inconsistent_link_is_quarantined(transfer):
    domain = Domain.objects.create(user=transfer.user, name=transfer.domain_name, tld="com", registrar_id="501")
    transfer.domain, transfer.registrar_order_id, transfer.status = domain, "501", "completed"
    transfer.save()
    seed()
    assert not DomainTransferAttempt.objects.exists()
    domain.registrar_id = "502"
    domain.save()
    seed()
    assert transfer.submission_attempt.legacy_review_required
    assert registrar_review_required(domain)


def test_modern_attempt_is_preserved_by_historical_scan(transfer, provider):
    submit(transfer, provider)
    seed()
    assert transfer.submission_attempt.status == "accepted" and not transfer.submission_attempt.legacy_review_required


def test_no_order_confirmation_releases_only_unlinked_historical_request(transfer, owner, provider):
    seed()
    review(transfer.submission_attempt, owner, "confirm-no-order")
    attempt = DomainTransferAttempt.objects.get()
    assert attempt.status == "aborted" and not attempt.legacy_review_required
    assert AuditLog.objects.filter(action="domain.transfer.reconciled", user=owner).exists()
    assert submit(transfer, provider) == "501"


def test_no_order_confirmation_cannot_erase_known_provider_id(transfer, owner):
    transfer.registrar_order_id = "501"
    transfer.save()
    seed()
    with pytest.raises(CommandError, match="unlinked historical"):
        review(transfer.submission_attempt, owner, "confirm-no-order")


def test_historical_adoption_is_readonly_and_still_does_not_complete_transfer(transfer, owner, provider, settings):
    seed()
    provider.get_registration_reconciliation_details.return_value = payload(transfer)
    with patch("apps.domains.management.commands.reconcile_transfer.ResellerClubClient", return_value=provider):
        review(transfer.submission_attempt, owner, "adopt-legacy", remote_order_id="501", customer_id="999")
    attempt = DomainTransferAttempt.objects.get()
    assert attempt.status == "accepted" and not attempt.legacy_review_required
    provider.transfer_domain.assert_not_called()
    provider.create_contact.assert_not_called()
    assert "DO-NOT-PERSIST" not in str(attempt.subject)
    assert not Domain.objects.exists()
    assert drive(transfer, provider, settings)
    domain = Domain.objects.get()
    assert domain.status == "pending" and domain.expires_at is None and not domain.nameserver1
    assert registrar_review_required(domain)
    provider.transfer_domain.assert_not_called()


@pytest.mark.parametrize("changes", [{"customerid": "888"}, {"domainname": "other.com"}, {"orderid": "502"}, {"billingcontactid": "999"}])
def test_modern_uncertain_link_requires_exact_provider_identity_and_roles(transfer, owner, provider, changes):
    provider.transfer_domain.side_effect = RuntimeError("connection lost")
    with pytest.raises(RuntimeError):
        submit(transfer, provider)
    provider.get_registration_reconciliation_details.return_value = payload(transfer, **changes)
    with patch("apps.domains.management.commands.reconcile_transfer.ResellerClubClient", return_value=provider), pytest.raises(CommandError):
        review(transfer.submission_attempt, owner, "link-order", remote_order_id="501")
    assert transfer.submission_attempt.status == "unknown"


def test_modern_link_saves_confirmed_order_without_purchase_or_local_activation(transfer, owner, provider):
    provider.transfer_domain.side_effect = RuntimeError("connection lost")
    with pytest.raises(RuntimeError):
        submit(transfer, provider)
    provider.get_registration_reconciliation_details.return_value = payload(transfer)
    with patch("apps.domains.management.commands.reconcile_transfer.ResellerClubClient", return_value=provider):
        review(transfer.submission_attempt, owner, "link-order", remote_order_id="501")
    assert DomainTransferAttempt.objects.get(transfer=transfer).status == "accepted" and provider.transfer_domain.call_count == 1
    assert submit(transfer, provider) == "501" and not Domain.objects.exists()


def test_stale_preparation_release_rotates_fence_and_never_releases_paid_marker(transfer, owner):
    attempt = DomainTransferAttempt.objects.create(transfer=transfer, status="preparing")
    original = attempt.token
    DomainTransferAttempt.objects.filter(pk=attempt.pk).update(updated_at=timezone.now() - timedelta(minutes=16))
    review(attempt, owner, "release-preparation")
    attempt.refresh_from_db()
    assert attempt.status == "aborted" and attempt.token != original
    attempt.status, attempt.submitted_at = "unknown", timezone.now()
    attempt.save()
    with pytest.raises(CommandError, match="Only preparation"):
        review(attempt, owner, "release-preparation")
    with pytest.raises(CommandError, match="matching historical or modern"):
        review(attempt, owner, "confirm-no-order")


def test_owner_revocation_during_provider_read_prevents_reconciliation(transfer, owner, provider):
    seed()
    def read(identifier):
        type(owner).objects.filter(pk=owner.pk).update(is_active=False)
        return payload(transfer)
    provider.get_registration_reconciliation_details.side_effect = read
    with patch("apps.domains.management.commands.reconcile_transfer.ResellerClubClient", return_value=provider), pytest.raises(CommandError, match="Owner access"):
        review(transfer.submission_attempt, owner, "adopt-legacy", remote_order_id="501", customer_id="999")
    assert transfer.submission_attempt.status == "unknown"


def test_audit_failure_rolls_back_recovery(transfer, owner):
    seed()
    with patch.object(AuditLog.objects, "create", side_effect=DatabaseError("PRIVATE-RESPONSE")), pytest.raises(CommandError) as error:
        review(transfer.submission_attempt, owner, "confirm-no-order")
    assert "PRIVATE-RESPONSE" not in str(error.value)
    assert transfer.submission_attempt.status == "unknown"


def test_historical_scan_is_batched_beyond_first_five_hundred(transfer):
    DomainTransfer.objects.bulk_create([DomainTransfer(user=transfer.user, domain_name=f"batch-{index}.com", tld="com",
        registration_contact=transfer.registration_contact, admin_contact=transfer.admin_contact,
        tech_contact=transfer.tech_contact, billing_contact=transfer.billing_contact) for index in range(501)])
    seed()
    assert DomainTransferAttempt.objects.filter(legacy_review_required=True).count() == 502
