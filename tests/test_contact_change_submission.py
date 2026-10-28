from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError

from apps.domains.contact_changes import submit_contact_change
from apps.domains.models import DomainContactChange
from tests.test_contact_change_admission import current, request
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return Mock(create_contact=Mock(return_value="301"),
        change_domain_contacts=Mock(return_value={"entityid": "501", "eaqid": "701"}),
        get_registration_reconciliation_details=Mock(return_value={"orderid": "501", "domainname": "paid-submission.com",
            "customerid": "999", "currentstatus": "Active", "registrantcontactid": "201", "admincontactid": "201",
            "techcontactid": "201", "billingcontactid": "201"}))


def test_submission_acknowledgement_does_not_replace_current_contacts(current, api):
    change = request(current)
    assert submit_contact_change(change.pk, client=api) == "awaiting"
    change.refresh_from_db()
    current[1].refresh_from_db()
    assert change.action_id == "701"
    assert change.role_ids["reg"] == "301"
    assert current[1].registrar_contact_ids["reg"] == "201"
    assert submit_contact_change(change.pk, client=api) == "awaiting"
    assert api.change_domain_contacts.call_count == 1
    assert api.create_contact.call_count == 1


def test_lost_submission_response_is_not_replayed(current, api):
    change = request(current)
    api.change_domain_contacts.side_effect = TimeoutError("private provider response")
    with pytest.raises(TimeoutError):
        submit_contact_change(change.pk, client=api)
    change.refresh_from_db()
    assert change.status == "unknown"
    assert submit_contact_change(change.pk, client=api) == "unknown"
    assert api.change_domain_contacts.call_count == 1
    assert "private" not in str(change.subject)


def test_foreign_live_order_stops_before_contact_creation(current, api):
    change = request(current)
    api.get_registration_reconciliation_details.return_value["customerid"] = "888"
    with pytest.raises(ValidationError):
        submit_contact_change(change.pk, client=api)
    api.create_contact.assert_not_called()
    api.change_domain_contacts.assert_not_called()
    assert DomainContactChange.objects.get(pk=change.pk).status == "failed"


def test_contact_edited_during_sync_stops_submission(current, api):
    change = request(current)
    def create(payload):
        contact = current[1].registration_contact
        contact.name = "Changed During Preparation"
        contact.save()
        return "301"
    api.create_contact.side_effect = create
    with pytest.raises(ValidationError):
        submit_contact_change(change.pk, client=api)
    api.change_domain_contacts.assert_not_called()
    assert DomainContactChange.objects.get(pk=change.pk).status == "failed"


def test_live_contact_change_between_reads_never_submits(current, api):
    change = request(current)
    original = api.get_registration_reconciliation_details.return_value
    api.get_registration_reconciliation_details.side_effect = [original, {**original, "registrantcontactid": "9999"}]
    with pytest.raises(ValidationError):
        submit_contact_change(change.pk, client=api)
    api.change_domain_contacts.assert_not_called()
    assert DomainContactChange.objects.get(pk=change.pk).status == "unknown"
