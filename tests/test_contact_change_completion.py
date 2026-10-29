from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.contact_change_confirmation import ContactChangeAction
from apps.domains.contact_change_recovery import confirm_provider_completed
from apps.domains.contact_changes import submit_contact_change
from apps.domains.models import DomainContactChange
from tests.test_contact_change_admission import current, request
from tests.test_contact_change_recovery import owner
from tests.test_contact_change_submission import api
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


@pytest.fixture
def completed_provider(current, api):
    change = request(current)
    submit_contact_change(change.pk, client=api)
    stale = timezone.now() - timedelta(minutes=20)
    DomainContactChange.objects.filter(pk=change.pk).update(submitted_at=stale)
    api.get_contact_change_action.return_value = ContactChangeAction("501", "701", "ModifyContact", "ReviewedProviderStatus",
        str(int((stale + timedelta(minutes=1)).timestamp())))
    api.get_irtp_status.return_value = {"order-id": "501", "domain-name": change.domain_name, "irtp-status": {
        "task-status": "SUCCESS", "losing-foa-status": "APPROVED", "gaining-foa-status": "APPROVED"}}
    for field in ("registrantcontactid", "admincontactid", "techcontactid", "billingcontactid"):
        api.get_registration_reconciliation_details.return_value[field] = "301"
    return change, api


def confirm(completed_provider, owner, **kwargs):
    change, api = completed_provider
    options = dict(owner_id=owner.pk, reference="CASE-701", reason="Provider confirmed successful completion of this exact contact-change action.",
        provider_success_confirmed=True, client=api)
    options.update(kwargs)
    return confirm_provider_completed(change.pk, **options)


def test_verified_provider_completion_applies_roles_and_preserves_paid_attempt(current, completed_provider, owner):
    change, api = completed_provider
    retained = dict(current[1].registration_attempt.subject)
    assert confirm(completed_provider, owner) == "confirmed"
    current[1].refresh_from_db()
    change.refresh_from_db()
    assert current[1].registrar_contact_ids["reg"] == "301"
    assert current[1].registration_attempt.subject == retained
    assert change.confirmed_at is not None and change.provider_observation["owner_attested_provider_success"]
    assert api.change_domain_contacts.call_count == api.create_contact.call_count == 1
    assert AuditLog.objects.filter(action="domain.contact_change.confirmed", user=owner).exists()


@pytest.mark.parametrize("state", ["PENDING", "APPROVED", "EXPIRED", "FAILED"])
def test_attestation_cannot_override_incomplete_irtp(current, completed_provider, owner, state):
    completed_provider[1].get_irtp_status.return_value["irtp-status"]["task-status"] = state
    with pytest.raises(ValidationError, match="authorization"):
        confirm(completed_provider, owner)
    current[1].refresh_from_db()
    assert current[1].registrar_contact_ids["reg"] == "201"


def test_publication_profile_change_cannot_skip_transfer_authorization(current, completed_provider, owner, monkeypatch):
    from dataclasses import replace
    from apps.domains.registrar_policy import POLICIES
    monkeypatch.setitem(POLICIES, "com", replace(POLICIES["com"], publication_workstream="registry_specific"))
    completed_provider[1].get_irtp_status.return_value["irtp-status"]["task-status"] = "PENDING"
    with pytest.raises(ValidationError, match="authorization"):
        confirm(completed_provider, owner)
    completed_provider[1].get_irtp_status.assert_called_once_with("501")
    current[1].refresh_from_db()
    assert current[1].registrar_contact_ids["reg"] == "201"


@pytest.mark.parametrize("timestamp", ["", "1", "9999999999999999999"])
def test_attestation_cannot_override_missing_or_unrelated_completion_time(completed_provider, owner, timestamp):
    api = completed_provider[1]
    api.get_contact_change_action.return_value = ContactChangeAction("501", "701", "ModifyContact", "Completed", timestamp)
    with pytest.raises(ValidationError, match="timestamp"):
        confirm(completed_provider, owner)


def test_attestation_cannot_override_actual_contact_roles(current, completed_provider, owner):
    completed_provider[1].get_registration_reconciliation_details.return_value["billingcontactid"] = "201"
    with pytest.raises(ValidationError, match="contacts changed"):
        confirm(completed_provider, owner)
    assert not AuditLog.objects.filter(action="domain.contact_change.confirmed").exists()


def test_explicit_provider_success_attestation_required(completed_provider, owner):
    with pytest.raises(ValidationError, match="attestation"):
        confirm(completed_provider, owner, provider_success_confirmed=False)


def test_changed_proposed_contact_cannot_be_applied_as_original(current, completed_provider, owner):
    contact = current[1].registration_contact
    contact.name = "Subsequent Unsynchronized Edit"
    contact.save()
    with pytest.raises(ValidationError, match="contact changed"):
        confirm(completed_provider, owner)


def test_live_worker_blocks_completion(completed_provider, owner):
    DomainContactChange.objects.filter(pk=completed_provider[0].pk).update(lease_until=timezone.now() + timedelta(minutes=1))
    with pytest.raises(ValidationError, match="no live worker"):
        confirm(completed_provider, owner)
