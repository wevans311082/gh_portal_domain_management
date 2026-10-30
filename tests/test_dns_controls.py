from datetime import timedelta
from unittest.mock import patch
import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.dns_changes import create_dns_preview
from apps.domains.dns_controls import cancel_dns_change
from apps.domains.dns_inventory import collect_dns_inventory
from apps.domains.dns_plan_review import create_record_plan, approve_record_plan
from apps.domains.models import DomainDNSChange, DomainDNSReservation, DomainDNSExecutionStep
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_inventory_stage import admitted
from tests.test_dns_switch_admission import preview, confirm

pytestmark = pytest.mark.django_db(transaction=True)


def cancel(change, **kwargs):
    values = dict(actor=change.requested_by, change_id=change.pk, manifest_digest=change.manifest_digest,
                  confirmed=True, reason="user_cancelled")
    values.update(kwargs)
    return cancel_dns_change(**values)


def test_draft_cancellation_is_offline_replayable_and_cannot_be_confirmed(current):
    change = preview(current)
    with patch("requests.request", side_effect=AssertionError("No provider calls")):
        result = cancel(change)
        assert result.cancelled_by_id == change.requested_by_id and result.cancelled_at
        assert cancel(change).cancelled_at == result.cancelled_at
        with pytest.raises(ValidationError, match="cancelled"):
            confirm(change)
    assert AuditLog.objects.filter(action="domain.dns.cancelled", object_id=str(change.pk)).count() == 1
    assert not DomainDNSReservation.objects.exists()


def test_confirmed_cancel_releases_only_its_reservation_and_allows_new_intent(admitted):
    change, source, target, registrar, domain = admitted
    result = cancel(change)
    assert result.status == "cancelled" and result.confirmed_at is not None
    assert not DomainDNSReservation.objects.filter(change=change).exists()
    fresh = create_dns_preview(actor=change.requested_by, domain_id=domain.pk, target_provider="cpanel",
        key=uuid.uuid4(), client=target, source_client=source)
    assert fresh.pk != change.pk and fresh.status == "draft"
    domain.refresh_from_db()
    assert domain.dns_provider == "cloudflare" and domain.cloudflare_zone_id == "b" * 32


def test_expired_approved_plan_can_be_cancelled_without_erasing_approval(admitted):
    change, source, target, registrar, domain = admitted
    collect_dns_inventory(change.pk, source_client=source, target_client=target, registrar_client=registrar)
    values = dict(source_client=source, target_client=target, registrar_client=registrar)
    plan = create_record_plan(actor=change.requested_by, change_id=change.pk, **values)
    approve_record_plan(actor=change.requested_by, plan_id=plan.pk, digest=plan.digest,
        confirmed=True, acknowledgements=plan.plan["required_acknowledgements"], **values)
    with patch("apps.domains.dns_controls.timezone.now", return_value=timezone.now() + timedelta(hours=1)):
        result = cancel(change, reason="replace_expired_plan")
    plan.refresh_from_db()
    assert result.approved_record_plan_id == plan.pk and result.inventory_digest and plan.approved_at
    assert result.cancellation_reason == "replace_expired_plan"


@pytest.mark.parametrize("field,value", [
    ("zone_submitted_at", "now"), ("zone_id", "retained-zone"),
    ("zone_observation", {"uncertain": True}), ("completed_at", "now"),
    ("status", "submitting"), ("status", "completed"), ("status", "unknown"),
])
def test_uncertain_or_completed_provider_work_cannot_release_slot(admitted, field, value):
    change = admitted[0]
    DomainDNSChange.objects.filter(pk=change.pk).update(**{field: timezone.now() if value == "now" else value})
    with pytest.raises(ValidationError, match="Reconcile"):
        cancel(change)
    assert DomainDNSReservation.objects.filter(change=change).exists()


def test_cancellation_during_inventory_fences_old_successful_observer(admitted):
    change, source, target, registrar, domain = admitted
    read = source.list_dns_records.side_effect
    def stop_during_read(*args, **kwargs):
        result = read(*args, **kwargs)
        cancel(change)
        return result
    source.list_dns_records.side_effect = stop_during_read
    result = collect_dns_inventory(change.pk, source_client=source, target_client=target, registrar_client=registrar)
    assert result.status == "cancelled" and result.inventory_token is None and result.inventory_lease_until is None
    assert not result.inventory_digest and not DomainDNSReservation.objects.filter(change=change).exists()


def test_old_read_cannot_publish_over_a_confirmed_replacement(admitted):
    change, source, target, registrar, domain = admitted
    read = source.list_dns_records.side_effect
    replacement = []
    def replace_during_read(*args, **kwargs):
        result = read(*args, **kwargs)
        cancel(change)
        fresh = create_dns_preview(actor=change.requested_by, domain_id=domain.pk, target_provider="cpanel",
            key=uuid.uuid4(), client=target, source_client=source)
        replacement.append(confirm(fresh, client=target, source_client=source))
        return result
    source.list_dns_records.side_effect = replace_during_read
    result = collect_dns_inventory(change.pk, source_client=source, target_client=target, registrar_client=registrar)
    assert result.status == "cancelled" and not result.inventory_receipt
    replacement[0].refresh_from_db()
    assert replacement[0].status == "inventory" and not replacement[0].inventory_receipt
    assert DomainDNSReservation.objects.get(domain=domain).change_id == replacement[0].pk


def test_cancellation_audit_failure_rolls_back_state_and_slot(admitted):
    change = admitted[0]
    with patch("apps.domains.dns_controls.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            cancel(change)
    change.refresh_from_db()
    assert change.status == "inventory" and change.cancelled_at is None
    assert DomainDNSReservation.objects.filter(change=change).exists()


def test_wrong_actor_revoked_actor_and_staff_cannot_cancel(admitted, django_user_model):
    change = admitted[0]
    for user in [django_user_model.objects.create_user(email="other-dns-cancel@example.test", password="testpass"),
                 django_user_model.objects.create_user(email="staff-dns-cancel@example.test", password="testpass", is_staff=True)]:
        with pytest.raises(PermissionDenied):
            cancel(change, actor=user)
    django_user_model.objects.filter(pk=change.requested_by_id).update(is_active=False)
    with pytest.raises(PermissionDenied):
        cancel(change)
    assert DomainDNSReservation.objects.filter(change=change).exists()


def test_owner_cancellation_preserves_original_attribution_on_customer_replay(admitted, django_user_model):
    change = admitted[0]
    owner = django_user_model.objects.create_user(email="owner-dns-cancel@example.test", password="testpass", is_staff=True, is_superuser=True)
    result = cancel(change, actor=owner)
    assert cancel(change).cancelled_by_id == owner.pk
    result.status = "inventory"
    with pytest.raises(ValidationError, match="cancelled"):
        result.save()
    with pytest.raises(ValidationError, match="cancellation"):
        cancel(change, reason="source_changed")


@pytest.mark.parametrize("values", [{"confirmed": "true"}, {"reason": "other"}, {"manifest_digest": "wrong"}])
def test_cancellation_requires_explicit_typed_confirmation_and_exact_intent(admitted, values):
    with pytest.raises(ValidationError):
        cancel(admitted[0], **values)
    assert DomainDNSReservation.objects.filter(change=admitted[0]).exists()


def test_missing_or_other_reservation_never_releases_an_unrelated_request(admitted):
    change = admitted[0]
    DomainDNSReservation.objects.filter(change=change).delete()
    with pytest.raises(ValidationError, match="reservation"):
        cancel(change)
    unrelated = DomainDNSChange.objects.create(domain=admitted[4], user=change.user, requested_by=change.requested_by,
        target_provider="cloudflare", provider_scope="c" * 64, source={}, request_digest="d" * 64,
        manifest_digest="e" * 64, expires_at=timezone.now())
    DomainDNSReservation.objects.create(domain=admitted[4], change=unrelated)
    with pytest.raises(ValidationError, match="reservation"):
        cancel(change)
    assert DomainDNSReservation.objects.filter(change=unrelated).exists()


def test_tampered_inventory_prevents_cancellation(admitted):
    change = admitted[0]
    DomainDNSChange.objects.filter(pk=change.pk).update(inventory_digest="f" * 64, inventory_receipt={"tampered": True})
    with pytest.raises(ValidationError, match="inventory"):
        cancel(change)
    assert DomainDNSReservation.objects.filter(change=change).exists()


def test_execution_submission_marker_holds_slot_even_if_parent_marker_is_missing(admitted):
    change = admitted[0]
    DomainDNSExecutionStep.objects.create(change=change, stage="record", identity="retained-record",
        created_by=change.requested_by, request={}, request_digest="d" * 64, submitted_at=timezone.now())
    change.refresh_from_db()
    assert change.zone_submitted_at is None and not change.zone_id
    with pytest.raises(ValidationError, match="Reconcile"):
        cancel(change)
    assert DomainDNSReservation.objects.filter(change=change).exists()
