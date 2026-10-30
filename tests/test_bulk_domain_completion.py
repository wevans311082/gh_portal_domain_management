from datetime import timedelta
from unittest.mock import patch
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.bulk_completion import confirm_lock_completed, EVENT
from apps.domains.bulk_remote import execute_remote_lock
from apps.domains.contact_change_confirmation import ContactChangeAction
from apps.domains.models import BulkDomainItem, DomainOperationReservation
from tests.test_bulk_domain_remote import Provider, admitted
from tests.test_bulk_domain_observation import Reader, evidence
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db(transaction=True)


class Reviewer(Reader):
    def __init__(self, item):
        super().__init__(item, evidence(item, []))
        self.action = ContactChangeAction("501", "812", "ReviewedUnlockType", "UnpublishedCode", str(int((timezone.now() - timedelta(minutes=10)).timestamp())))

    def get_contact_change_action(self, order_id, action_id):
        self.reads.append((order_id, action_id))
        return self.action


def review_setup(current):
    item = admitted(current)
    execute_remote_lock(item.pk, client=Provider(item))
    owner = User.objects.create_user(email="bulk-completion-owner@example.test", password="test", is_staff=True, is_superuser=True)
    from apps.domains.bulk_controls import set_bulk_paused
    set_bulk_paused(item.pk, owner_id=owner.pk, paused=True, reason="Pause this item for exact provider action review.")
    earlier = timezone.now() - timedelta(minutes=20)
    BulkDomainItem.objects.filter(pk=item.pk).update(submitted_at=earlier, updated_at=earlier)
    item.refresh_from_db()
    return item, owner, Reviewer(item)


def review(item, owner, client, **kwargs):
    values = dict(owner_id=owner.pk, action_id="812", action_type="ReviewedUnlockType", reference="CASE-001", reason="Reviewed exact provider action after stopping workers.", provider_success_confirmed=True, workers_stopped_confirmed=True, requested_operation_confirmed=True, client=client)
    values.update(kwargs)
    return confirm_lock_completed(item.pk, **values)


def test_review_requires_terminal_action_and_live_target_then_finishes_once(current):
    item, owner, client = review_setup(current)
    result = review(item, owner, client)
    assert result.status == "completed" and result.observed_lock is False
    assert not DomainOperationReservation.objects.filter(item=item).exists()
    current[0].refresh_from_db()
    assert current[0].is_locked is False
    reads = list(client.reads)
    assert review(item, owner, client).status == "completed"
    assert client.reads == reads
    receipt = AuditLog.objects.get(action=EVENT)
    assert receipt.user_id == owner.pk and receipt.data["owner_attested_exact_requested_operation"] is True
    assert "CASE-001" not in result.message
    with pytest.raises(ValidationError, match="differs"):
        review(item, owner, client, reference="CASE-002")


@pytest.mark.parametrize("field,value", [("provider_success_confirmed", False), ("workers_stopped_confirmed", False), ("requested_operation_confirmed", False), ("provider_success_confirmed", "true"), ("reason", "short"), ("reference", "invalid case")])
def test_explicit_bounded_review_required_before_provider_reads(current, field, value):
    item, owner, client = review_setup(current)
    with pytest.raises(ValidationError):
        review(item, owner, client, **{field: value})
    assert not client.reads and DomainOperationReservation.objects.filter(item=item).exists()


@pytest.mark.parametrize("field,value", [("order_id", "999"), ("action_id", "999"), ("action_type", "DifferentType"), ("completed_at", ""), ("completed_at", "1"), ("completed_at", "9999999999")])
def test_unmatched_or_nonterminal_action_cannot_be_overridden_by_owner(current, field, value):
    from dataclasses import replace
    item, owner, client = review_setup(current)
    client.action = replace(client.action, **{field: value})
    with pytest.raises(ValidationError, match="terminal timestamp"):
        review(item, owner, client)
    item.refresh_from_db()
    assert item.status == "awaiting" and DomainOperationReservation.objects.filter(item=item).exists()


def test_matching_action_without_live_target_cannot_finish(current):
    item, owner, client = review_setup(current)
    client.payload = evidence(item, ["transferlock"])
    with pytest.raises(ValidationError, match="live registrar lock"):
        review(item, owner, client)
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_recent_activity_and_nonowner_are_denied_without_reads(current):
    item, owner, client = review_setup(current)
    BulkDomainItem.objects.filter(pk=item.pk).update(updated_at=timezone.now())
    with pytest.raises(ValidationError, match="15 minutes"):
        review(item, owner, client)
    owner.is_superuser = False
    owner.save(update_fields=["is_superuser"])
    with pytest.raises(ValidationError, match="active owner"):
        review(item, owner, client)
    assert not client.reads


def test_owner_revocation_during_read_and_audit_failure_retain_reservation(current):
    item, owner, client = review_setup(current)
    client.hook = lambda: User.objects.filter(pk=owner.pk).update(is_active=False)
    with pytest.raises(ValidationError, match="Owner access"):
        review(item, owner, client)
    item.refresh_from_db()
    assert item.status == "awaiting" and DomainOperationReservation.objects.filter(item=item).exists()


def test_completion_audit_failure_rolls_back_lock_and_slot_release(current):
    item, owner, client = review_setup(current)
    real_create = AuditLog.objects.create
    def audit(**kwargs):
        if kwargs["action"] == EVENT:
            raise RuntimeError("audit unavailable")
        return real_create(**kwargs)
    with patch("apps.domains.bulk_completion.AuditLog.objects.create", side_effect=audit):
        with pytest.raises(RuntimeError):
            review(item, owner, client)
    item.refresh_from_db()
    current[0].refresh_from_db()
    assert item.status == "awaiting" and current[0].is_locked is True
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_start_audit_failure_prevents_fencing_and_provider_reads(current):
    item, owner, client = review_setup(current)
    token = item.token
    with patch("apps.domains.bulk_completion.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            review(item, owner, client)
    item.refresh_from_db()
    assert item.token == token and item.lease_until is None and not client.reads


def test_refreshed_local_cache_does_not_replace_required_terminal_live_evidence(current):
    item, owner, client = review_setup(current)
    current[0].is_locked = False
    current[0].save(update_fields=["is_locked"])
    assert review(item, owner, client).status == "completed"
    assert not DomainOperationReservation.objects.filter(item=item).exists()


def test_owner_can_link_case_reviewed_unknown_action_and_fence_old_callback(current):
    from apps.domains.bulk_remote import _record_remote_result
    item, owner, client = review_setup(current)
    previous_token = item.token
    BulkDomainItem.objects.filter(pk=item.pk).update(status="unknown", provider_action_id="")
    result = review(item, owner, client)
    assert result.status == "completed" and result.provider_action_id == "812"
    assert result.token != previous_token
    assert _record_remote_result(item.pk, previous_token, "999").status == "completed"
    result.refresh_from_db()
    assert result.provider_action_id == "812"


def test_association_change_during_read_cannot_be_overridden_by_review(current):
    item, owner, client = review_setup(current)
    def changed():
        current[0].name = "changed-association.com"
        current[0].save(update_fields=["name"])
    client.hook = changed
    with pytest.raises(ValidationError):
        review(item, owner, client)
    item.refresh_from_db()
    assert item.status == "awaiting" and DomainOperationReservation.objects.filter(item=item).exists()
