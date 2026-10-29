from tests.agreement_helpers import attach_agreement
from datetime import datetime, timezone as datetime_timezone
import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.management import call_command, CommandError
from django.db import connection, DatabaseError
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.domains.models import Domain, DomainOrder, DomainRegistrationAttempt
from apps.domains.registration_submission import RegistrationReviewRequired
from tests.test_registration_submission import order, provider, submit, NS
from tests.security_helpers import login_owner

pytestmark = pytest.mark.django_db


def seed():
    module = importlib.import_module("apps.domains.migrations.0017_historical_registration_review")
    module.quarantine_historical_orders(apps, SimpleNamespace(connection=connection))


def payload(order, **changes):
    values = {"entityid": "501", "domainname": order.domain_name, "customerid": "999", "currentstatus": "Suspended",
              "registrantcontactid": "201", "admincontactid": "202", "techcontactid": "203", "billingcontactid": "204",
              "noOfNameServers": "2", "ns1": NS[0], "ns2": NS[1],
              "creationtime": str(int(datetime(2020, 1, 1, tzinfo=datetime_timezone.utc).timestamp())),
              "endtime": str(int(datetime(2030, 1, 1, tzinfo=datetime_timezone.utc).timestamp())),
              "domsecret": "PRIVATE-PROVIDER-SECRET", "registrantcontact": {"name": "PRIVATE CONTACT BODY"}}
    values.update(changes)
    return values


def owner_account(django_user_model):
    return django_user_model.objects.create_superuser(email="historical-owner@example.test", password="testpass")


def review(attempt, owner, action="confirm-no-order", **changes):
    values = dict(attempt_id=attempt.pk, owner_id=owner.pk, action=action, provider_reference="provider-case-123",
                  reason="Provider support reviewed the historical domain and original purchase state.")
    values.update(changes)
    return call_command("reconcile_registration", **values)


@pytest.mark.parametrize("status", ["draft", "pending_payment", "paid", "processing", "failed", "paused", "cancelled", "completed"])
def test_every_unconfirmed_historical_status_requires_review_without_contact_or_provider_work(order, provider, status):
    order.status = status
    order.last_error = "PRIVATE OLD RESPONSE"
    order.registrar_order_id = "PRIVATE INVALID IDENTIFIER"
    order.save()
    with patch("apps.domains.models.DomainContact.as_resellerclub_payload", side_effect=AssertionError("migration must not validate contacts")):
        seed()
    seed()  # Idempotent: no overwriting a retained reservation.
    attempt = DomainRegistrationAttempt.objects.get(order=order)
    assert attempt.legacy_review_required and attempt.status == "unknown"
    assert attempt.provider_scope == attempt.customer_id == ""
    assert "PRIVATE" not in str(attempt.__dict__)
    assert attempt.subject["legacy_original_status"] == status
    with pytest.raises(RegistrationReviewRequired):
        submit(order, provider)
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()


def test_consistently_completed_existing_asset_is_preserved(order, django_user_model):
    domain = Domain.objects.create(user=order.user, name=order.domain_name, tld="com", registrar_id="501", status="active")
    order.status, order.registrar_order_id, order.domain = "completed", "501", domain
    order.save()
    seed()
    assert not DomainRegistrationAttempt.objects.filter(order=order).exists()
    assert Domain.objects.get(pk=domain.pk).status == "active"


def test_early_unverified_legacy_bootstrap_is_not_mistaken_for_a_paid_acknowledgement(order):
    from apps.domains.historical_registration import historical_subject
    order.registrar_order_id = "501"
    order.save()
    attempt = DomainRegistrationAttempt.objects.create(order=order, status="accepted", remote_order_id="501", customer_id="999", provider_scope="unconfirmed", subject=historical_subject(order))
    AuditLog.objects.create(action="domain.registration.legacy_link_retained", model_name="DomainOrder", object_id=str(order.pk), data={"registration_attempt_id": attempt.pk})
    seed()
    attempt.refresh_from_db()
    assert attempt.status == "unknown" and attempt.legacy_review_required and not attempt.remote_order_id


def test_real_paid_acknowledgement_is_preserved_even_with_older_legacy_audit_entries(order, provider):
    attach_agreement(order)
    submit(order, provider)
    AuditLog.objects.create(action="domain.registration.legacy_link_retained", model_name="DomainOrder", object_id=str(order.pk))
    seed()
    attempt = DomainRegistrationAttempt.objects.get()
    assert attempt.status == "accepted" and attempt.submitted_at and not attempt.legacy_review_required


def test_inconsistent_completed_asset_cannot_bypass_review_in_worker(order, provider, django_user_model):
    from apps.domains.tasks import register_domain_order
    other = django_user_model.objects.create_user(email="foreign-legacy@example.test", password="testpass")
    domain = Domain.objects.create(user=other, name=order.domain_name, tld="com", registrar_id="501")
    order.status, order.registrar_order_id, order.domain = "completed", "501", domain
    order.save()
    seed()
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), pytest.raises(RegistrationReviewRequired):
        register_domain_order.run(order.pk)
    provider.register_domain.assert_not_called()


def test_backfill_does_not_overwrite_modern_attempts_and_batches_large_histories(order, provider):
    from apps.domains.registration_submission import _claim
    attach_agreement(order)
    attempt, _ = _claim(order.pk, "999", NS, provider)
    rows = [DomainOrder(user=order.user, domain_name=f"old-{i}.com", tld="com", registration_contact=order.registration_contact,
        admin_contact=order.admin_contact, tech_contact=order.tech_contact, billing_contact=order.billing_contact) for i in range(501)]
    DomainOrder.objects.bulk_create(rows)
    seed()
    attempt.refresh_from_db()
    assert attempt.status == "preparing" and not attempt.legacy_review_required
    assert DomainRegistrationAttempt.objects.filter(legacy_review_required=True).count() == 501


def test_no_order_confirmation_is_audited_but_does_not_itself_purchase(order, provider, django_user_model):
    seed()
    attempt = DomainRegistrationAttempt.objects.get(order=order)
    owner = owner_account(django_user_model)
    with patch("apps.domains.management.commands.reconcile_registration.ResellerClubClient") as client:
        review(attempt, owner)
        client.assert_not_called()
    attempt.refresh_from_db()
    assert not attempt.legacy_review_required and attempt.status == "aborted"
    assert AuditLog.objects.filter(action="domain.registration.historical_reviewed", user=owner).count() == 1
    provider.register_domain.assert_not_called()
    attach_agreement(order)
    assert submit(order, provider) == "501"
    assert provider.register_domain.call_count == 1


def test_known_order_cannot_be_released_as_no_previous_purchase(order, django_user_model):
    order.registrar_order_id = "501"
    order.save()
    seed()
    with pytest.raises(CommandError, match="known registrar order"):
        review(DomainRegistrationAttempt.objects.get(), owner_account(django_user_model))


def test_existing_historical_order_adoption_is_read_only_and_retains_only_safe_facts(order, provider, django_user_model):
    seed()
    attempt = DomainRegistrationAttempt.objects.get()
    provider.get_registration_reconciliation_details.return_value = payload(order)
    with patch("apps.domains.management.commands.reconcile_registration.ResellerClubClient", return_value=provider):
        review(attempt, owner_account(django_user_model), "adopt-legacy", remote_order_id="501", customer_id="999")
    attempt.refresh_from_db()
    order.refresh_from_db()
    assert not attempt.legacy_review_required and attempt.status == "accepted"
    assert order.registrar_order_id == "501" and order.registrar_contact_ids["reg"] == "201"
    assert attempt.registry_status == "suspended" and attempt.registry_registered_at.isoformat() == "2020-01-01"
    assert "PRIVATE" not in str(attempt.__dict__)
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"domainname": "another.com"}, {"customerid": "998"}, {"entityid": "502"}, {"registrantcontactid": True},
    {"admincontactid": ""}, {"currentstatus": "Deleted"}, {"currentstatus": {}}, {"creationtime": True},
    {"endtime": "1"}, {"noOfNameServers": 99}, {"noOfNameServers": True}, {"ns2": "invalid host"},
    {"ns2": NS[0]}, {"creationtime": "9999999999999999999"},
])
def test_malformed_or_mismatched_provider_facts_never_resolve_historical_review(order, provider, django_user_model, changes):
    seed()
    attempt = DomainRegistrationAttempt.objects.get()
    provider.get_registration_reconciliation_details.return_value = payload(order, **changes)
    with patch("apps.domains.management.commands.reconcile_registration.ResellerClubClient", return_value=provider):
        with pytest.raises(CommandError, match="could not be confirmed"):
            review(attempt, owner_account(django_user_model), "adopt-legacy", remote_order_id="501", customer_id="999")
    attempt.refresh_from_db()
    assert attempt.legacy_review_required and not attempt.remote_order_id
    provider.register_domain.assert_not_called()


def test_adopted_suspended_asset_uses_fresh_provider_status_and_dates_without_purchase(order, provider, django_user_model, settings):
    from apps.domains.tasks import register_domain_order
    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    seed()
    provider.get_registration_reconciliation_details.return_value = payload(order)
    with patch("apps.domains.management.commands.reconcile_registration.ResellerClubClient", return_value=provider):
        review(DomainRegistrationAttempt.objects.get(), owner_account(django_user_model), "adopt-legacy", remote_order_id="501", customer_id="999")
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch("apps.domains.tasks._build_nameservers", return_value=NS):
        result = register_domain_order.run(order.pk)
    domain = Domain.objects.get(pk=result)
    assert domain.status == "suspended" and domain.registered_at.isoformat() == "2020-01-01" and domain.expires_at.isoformat() == "2030-01-01"
    assert provider.get_registration_reconciliation_details.call_count == 2
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()


def test_inactive_provider_order_keeps_unknown_dates_and_delegation_and_does_not_provision_dns(order, provider, django_user_model, settings):
    from apps.domains.tasks import register_domain_order
    from apps.dns.models import DNSZone
    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    order.dns_provider = "cloudflare"
    order.save()
    seed()
    provider.get_registration_reconciliation_details.return_value = payload(order, currentstatus="InActive", creationtime="0", endtime="0", noOfNameServers="0", ns1=None, ns2=None)
    with patch("apps.domains.management.commands.reconcile_registration.ResellerClubClient", return_value=provider):
        review(DomainRegistrationAttempt.objects.get(), owner_account(django_user_model), "adopt-legacy", remote_order_id="501", customer_id="999")
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch("apps.domains.tasks._build_nameservers") as ns, patch("apps.domains.tasks._ensure_cloudflare_zone") as dns:
        result = register_domain_order.run(order.pk)
        ns.assert_not_called()
        dns.assert_not_called()
    domain = Domain.objects.get(pk=result)
    assert domain.status == "pending" and domain.registered_at is None and domain.expires_at is None
    assert not domain.nameserver1 and not DNSZone.objects.get(domain=domain).is_active
    provider.register_domain.assert_not_called()


def test_owner_revocation_during_provider_read_prevents_historical_adoption(order, provider, django_user_model):
    seed()
    owner = owner_account(django_user_model)
    def read(identifier):
        django_user_model.objects.filter(pk=owner.pk).update(is_active=False)
        return payload(order)
    provider.get_registration_reconciliation_details.side_effect = read
    with patch("apps.domains.management.commands.reconcile_registration.ResellerClubClient", return_value=provider):
        with pytest.raises(CommandError, match="Owner access changed"):
            review(DomainRegistrationAttempt.objects.get(), owner, "adopt-legacy", remote_order_id="501", customer_id="999")
    assert DomainRegistrationAttempt.objects.get().legacy_review_required


def test_adopted_order_keeps_its_provider_customer_scope_after_default_customer_changes(order, provider, django_user_model, settings):
    from apps.domains.tasks import register_domain_order
    from apps.domains.verification import reconcile_registrant_verification
    settings.RESELLERCLUB_CUSTOMER_ID = "1000"
    seed()
    provider.get_registration_reconciliation_details.return_value = payload(order)
    with patch("apps.domains.management.commands.reconcile_registration.ResellerClubClient", return_value=provider):
        review(DomainRegistrationAttempt.objects.get(), owner_account(django_user_model), "adopt-legacy", remote_order_id="501", customer_id="999")
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider):
        assert register_domain_order.run(order.pk)
    provider.get_registrant_verification_details.return_value = {
        "orderid": "501", "domainname": order.domain_name, "customerid": "999", "registrantcontactid": "201",
        "registrantcontact": {"email": order.registration_contact.email}, "raaVerificationStatus": "Verified",
    }
    evidence = reconcile_registrant_verification(order.pk, "1000", client=provider)
    assert evidence.is_verified
    provider.register_domain.assert_not_called()


def test_historical_review_cannot_be_bypassed_by_the_email_verification_reader(order, provider):
    from django.core.exceptions import ValidationError
    from apps.domains.verification import reconcile_registrant_verification
    order.registrar_order_id = "501"
    order.save()
    seed()
    with pytest.raises(ValidationError, match="Historical owner review"):
        reconcile_registrant_verification(order.pk, "999", client=provider)
    provider.get_registrant_verification_details.assert_not_called()


def test_pasted_id_from_another_namespace_is_quarantined_before_any_provider_mutation(order, provider):
    order.registrar_order_id = "501"
    order.registrar_contact_ids = {"_scope": "old-unconfirmed-namespace", "reg": "201"}
    order.save()
    with pytest.raises(RegistrationReviewRequired):
        submit(order, provider)
    assert DomainRegistrationAttempt.objects.get().legacy_review_required
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()


def test_historical_diagnostics_do_not_leak_through_detail_list_or_search(order, client, django_user_model):
    owner = owner_account(django_user_model)
    login_owner(client, owner)
    order.registrar_order_id = "PRIVATE-OLD-PROVIDER-REPLY"
    order.last_error = "PRIVATE-OLD-PROVIDER-DIAGNOSTIC"
    order.save()
    seed()
    with patch("apps.domains.tasks._build_nameservers") as resolver:
        response = client.get(reverse("admin_tools:domain_order_detail", args=[order.pk]))
        resolver.assert_not_called()
    assert b"Historical submission not confirmed" in response.content and b"PRIVATE-OLD" not in response.content
    url = reverse("admin_tools:domain_orders_list")
    assert b"PRIVATE-OLD" not in client.get(url).content
    assert client.get(url, {"q": "PRIVATE-OLD"}).context["page_obj"].paginator.count == 0


def test_customer_sees_only_owned_review_notices_without_diagnostics_or_provider_ids(order, client, django_user_model):
    order.last_error = "PRIVATE-OLD-DIAGNOSTIC"
    order.registrar_order_id = "PRIVATE-OLD-ID"
    order.save()
    other = django_user_model.objects.create_user(email="other-notice-owner@example.test", password="testpass")
    DomainOrder.objects.create(user=other, domain_name="foreign-review.com", tld="com", registration_contact=order.registration_contact,
        admin_contact=order.admin_contact, tech_contact=order.tech_contact, billing_contact=order.billing_contact)
    seed()
    client.force_login(order.user)
    response = client.get(reverse("domains:my_domains"))
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert b"Registration awaiting review" in response.content and b"paid-submission.com" in response.content
    assert b"PRIVATE-OLD" not in response.content and b"foreign-review.com" not in response.content


def test_review_cannot_be_cleared_with_modern_commands_or_changed_local_identity(order, provider, django_user_model):
    seed()
    owner = owner_account(django_user_model)
    attempt = DomainRegistrationAttempt.objects.get()
    with pytest.raises(CommandError, match="dedicated owner review"):
        review(attempt, owner, "release-preparation")
    DomainOrder.objects.filter(pk=order.pk).update(registration_years=2)
    with pytest.raises(CommandError, match="historical order changed"):
        review(attempt, owner)


def test_audit_failure_rolls_back_historical_release(order, django_user_model):
    seed()
    attempt = DomainRegistrationAttempt.objects.get()
    with patch("apps.audit.models.AuditLog.objects.create", side_effect=DatabaseError("private diagnostic")):
        with pytest.raises(CommandError, match="did not complete"):
            review(attempt, owner_account(django_user_model))
    attempt.refresh_from_db()
    assert attempt.legacy_review_required and attempt.status == "unknown"


def test_provider_read_requests_only_the_documented_reconciliation_options():
    from apps.domains.resellerclub_client import ResellerClubClient
    client = object.__new__(ResellerClubClient)
    client._get = lambda endpoint, parameters: {"orders.orderid": "501", "options": parameters["options"]}
    result = client.get_registration_reconciliation_details("501")
    assert result["orderid"] == "501"
    assert result["options"] == ["OrderDetails", "ContactIds", "NsDetails", "StatusDetails"]
