from unittest.mock import patch
import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from apps.domains.bulk_observation import observed_lock
from apps.domains.bulk_reconciliation import observe_remote_lock
from apps.domains.bulk_remote import execute_remote_lock
from apps.domains.models import BulkDomainItem, DomainOperationReservation
from tests.test_bulk_domain_remote import Provider, admitted
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db(transaction=True)


def evidence(item, flags):
    return {"orderid": "501", "entityid": "501", "customerid": "999", "domainname": item.name,
        "currentstatus": "Active", "orderstatus": flags,
        "registrantcontactid": "201", "admincontactid": "201", "techcontactid": "201", "billingcontactid": "201",
        "domsecret": "PRIVATE-READ-BODY"}


class Reader(Provider):
    def __init__(self, item, payload, hook=None):
        super().__init__(item)
        self.payload, self.hook, self.reads = payload, hook, []

    def get_registration_reconciliation_details(self, order_id):
        assert not connection.in_atomic_block
        self.reads.append(order_id)
        if self.hook:
            self.hook()
        return self.payload


@pytest.mark.parametrize("flags,expected", [("transferlock", True), (["transferlock"], True), ([], False)])
def test_only_explicit_matched_flags_form_lock_evidence(current, flags, expected):
    item = admitted(current)
    assert observed_lock(evidence(item, flags), item.snapshot) is expected


@pytest.mark.parametrize("field,value", [("orderid", "999"), ("entityid", "999"), ("customerid", "111"), ("domainname", "foreign.test"), ("currentstatus", "Suspended"), ("admincontactid", "222"), ("orderstatus", None), ("orderstatus", ""), ("orderstatus", ["unknown"]), ("orderstatus", ["resellerlock"]), ("orderstatus", ["transferlock", "transferlock"])])
def test_wrong_identity_ambiguous_or_additional_holds_require_review(current, field, value):
    item = admitted(current)
    payload = evidence(item, [])
    payload[field] = value
    with pytest.raises(ValidationError):
        observed_lock(payload, item.snapshot)


def test_matching_live_state_does_not_prove_queued_action_completion(current):
    item = admitted(current)
    execute_remote_lock(item.pk, client=Provider(item))
    reader = Reader(item, evidence(item, []))
    result = observe_remote_lock(item.pk, client=reader)
    assert result.status == "awaiting" and result.observed_lock is False and result.checked_at is not None
    assert result.completed_at is None and DomainOperationReservation.objects.filter(item=item).exists()
    current[0].refresh_from_db()
    assert current[0].is_locked is True
    assert "PRIVATE-READ-BODY" not in result.message
    observe_remote_lock(item.pk, client=reader)
    assert reader.reads == ["501"]  # Bounded observation cooldown.


def test_interrupted_submitting_call_is_not_observed_as_finished(current):
    item = admitted(current)
    BulkDomainItem.objects.filter(pk=item.pk).update(status="submitting")
    reader = Reader(item, evidence(item, []))
    assert observe_remote_lock(item.pk, client=reader).status == "submitting"
    assert not reader.reads


def test_invalid_read_retains_unknown_reservation_and_clears_lease(current):
    item = admitted(current)
    execute_remote_lock(item.pk, client=Provider(item))
    result = observe_remote_lock(item.pk, client=Reader(item, {"domsecret": "PRIVATE-READ-BODY"}))
    assert result.status == "unknown" and result.observed_lock is None and result.lease_until is None
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_subject_changed_during_read_does_not_become_evidence(current):
    item = admitted(current)
    execute_remote_lock(item.pk, client=Provider(item))
    def change():
        current[0].name = "changed.com"
        current[0].save(update_fields=["name"])
    result = observe_remote_lock(item.pk, client=Reader(item, evidence(item, []), hook=change))
    assert result.status == "unknown" and result.observed_lock is None
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_audit_failure_rolls_back_observation_and_keeps_read_lease(current):
    item = admitted(current)
    execute_remote_lock(item.pk, client=Provider(item))
    with patch("apps.domains.bulk_reconciliation.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            observe_remote_lock(item.pk, client=Reader(item, evidence(item, [])))
    item.refresh_from_db()
    assert item.observed_lock is None and item.checked_at is None and item.lease_until is not None
    assert DomainOperationReservation.objects.filter(item=item).exists()
