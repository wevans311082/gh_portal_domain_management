import pytest
from django.core.exceptions import ValidationError

from apps.domains.contact_change_confirmation import ContactChangeAction
from apps.domains.contact_changes import observe_contact_change, submit_contact_change
from apps.domains.models import DomainContactChange
from tests.test_contact_change_admission import current, request
from tests.test_contact_change_submission import api
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


def pending(current, api, state="PENDING"):
    change = request(current)
    submit_contact_change(change.pk, client=api)
    api.get_contact_change_action.return_value = ContactChangeAction("501", "701", "ModifyContact", "AwaitingIRTP", "")
    api.get_irtp_status.return_value = {"order-id": "501", "domain-name": change.domain_name, "irtp-status": {
        "task-status": state, "losing-foa-status": "APPROVED", "gaining-foa-status": "APPROVED"}}
    return change


@pytest.mark.parametrize("state", ["PENDING", "APPROVED", "SUCCESS", "EXPIRED"])
def test_observations_do_not_imply_local_completion(current, api, state):
    change = pending(current, api, state)
    assert observe_contact_change(change.pk, client=api) == "awaiting"
    change.refresh_from_db()
    assert change.irtp_status == state
    assert change.checked_at and change.available_at > change.checked_at
    assert change.provider_observation["roles_match"] is False
    assert api.change_domain_contacts.call_count == api.create_contact.call_count == 1


def test_matching_new_roles_are_recorded_without_mutating_old_association(current, api):
    change = pending(current, api, "SUCCESS")
    for field in ("registrantcontactid", "admincontactid", "techcontactid", "billingcontactid"):
        api.get_registration_reconciliation_details.return_value[field] = "301"
    observe_contact_change(change.pk, client=api)
    change.refresh_from_db()
    current[1].refresh_from_db()
    assert change.provider_observation["roles_match"] is True
    assert current[1].registrar_contact_ids["reg"] == "201"


def test_local_contact_edit_does_not_hide_provider_status(current, api):
    change = pending(current, api)
    contact = current[1].registration_contact
    contact.name = "Locally Edited Holder"
    contact.save()
    observe_contact_change(change.pk, client=api)
    assert DomainContactChange.objects.get(pk=change.pk).checked_at is not None
    assert api.change_domain_contacts.call_count == 1


def test_foreign_live_identity_is_not_saved_as_pending(current, api):
    change = pending(current, api)
    api.get_registration_reconciliation_details.return_value["customerid"] = "888"
    with pytest.raises(ValidationError):
        observe_contact_change(change.pk, client=api)
    assert DomainContactChange.objects.get(pk=change.pk).checked_at is None


def test_foreign_action_is_not_saved(current, api):
    change = pending(current, api)
    api.get_contact_change_action.return_value = ContactChangeAction("501", "702", "ModifyContact", "Completed", "")
    with pytest.raises(ValidationError):
        observe_contact_change(change.pk, client=api)
    assert DomainContactChange.objects.get(pk=change.pk).checked_at is None
