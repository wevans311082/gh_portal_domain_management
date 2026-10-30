from datetime import timedelta
from unittest.mock import patch
import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.cloudflare_integration.services import CloudflareError
from apps.domains.contact_bindings import _fingerprint
from apps.domains.dns_changes import create_dns_preview
from apps.domains.dns_zone_execution import inspect_cloudflare_zone_submission
from apps.domains.dns_zone_recovery import resolve_cloudflare_zone, cancel_prepared_dns_change
from apps.domains.models import DomainDNSExecutionStep, DomainDNSReservation
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_inventory_stage import admitted
from tests.test_dns_zone_execution import prepared_input, build_prepared_input, execute
from tests.test_cloudflare_zones import zone, page as zone_page
from tests.test_cloudflare_inventory import row, page as record_page
from tests.test_dns_switch_admission import confirm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def uncertain(admitted, monkeypatch):
    prepared = build_prepared_input(admitted, "create", monkeypatch)
    values, state = prepared
    values[2].create_managed_zone.side_effect = CloudflareError("timeout", uncertain=True)
    assert execute(prepared).status == "review"
    state["exists"] = True
    step = inspect_cloudflare_zone_submission(actor=values[0].requested_by, change_id=values[0].pk, target_client=values[2])
    return prepared, step


def resolve(uncertain, **kwargs):
    (values, state), step = uncertain
    arguments = dict(actor=values[0].requested_by, change_id=values[0].pk, confirmed=True, zone_id="b" * 32,
        observation_digest=step.observations[-1]["digest"], target_client=values[2])
    arguments.update(kwargs)
    return resolve_cloudflare_zone(**arguments)


def cancel_prepared(prepared_input, step, **kwargs):
    values, _ = prepared_input
    arguments = dict(actor=values[0].requested_by, change_id=values[0].pk, manifest_digest=values[0].manifest_digest,
        receipt_digest=step.receipt_digest, confirmed=True, keep_zone=True, reason="user_cancelled")
    arguments.update(kwargs)
    return cancel_prepared_dns_change(**arguments)


def test_explicit_adoption_resolves_preparation_without_claiming_create_provenance(uncertain):
    prepared, step = uncertain
    result = resolve(uncertain)
    values, _ = prepared
    assert result.status == "complete" and result.submitted_at == step.submitted_at and result.provider_id == "b" * 32
    assert result.receipt["resolution"]["original_submission_outcome"] == "unproven"
    assert result.receipt["resolution"]["kind"] == "adopt_observed_zone"
    assert result.receipt["zone_preparation_complete"] is True and result.receipt["provider_action_complete"] is False
    assert result.receipt["delegation_updated"] is False
    count = values[2].list_zones.call_count
    assert resolve(uncertain).receipt_digest == result.receipt_digest
    assert values[2].list_zones.call_count == count and values[2].create_managed_zone.call_count == 1
    assert execute(prepared).receipt_digest == result.receipt_digest
    values[4].refresh_from_db()
    assert values[4].dns_provider == "cpanel" and DomainDNSReservation.objects.filter(change=values[0]).exists()


@pytest.mark.parametrize("changes", [{"confirmed": "true"}, {"confirmed": False}, {"zone_id": "c" * 32}, {"observation_digest": "wrong"}])
def test_resolution_requires_exact_current_observation_and_real_consent(uncertain, changes):
    with pytest.raises(ValidationError):
        resolve(uncertain, **changes)
    assert not DomainDNSExecutionStep.objects.get(pk=uncertain[1].pk).receipt_digest


def test_full_old_inspection_history_can_recover_only_through_fresh_matching_readback(uncertain):
    prepared, step = uncertain
    step.observations = step.observations * 20
    step.save(update_fields=["observations"])
    count = prepared[0][2].list_zones.call_count
    with patch("apps.domains.dns_zone_recovery.timezone.now", return_value=timezone.now() + timedelta(hours=1)):
        result = resolve(uncertain)
    assert result.status == "complete" and len(result.observations) == 20
    assert prepared[0][2].list_zones.call_count > count and prepared[0][2].create_managed_zone.call_count == 1


def test_fresh_inspection_can_resolve_old_effect_without_renewing_execution_consent(uncertain):
    prepared, step = uncertain
    values, _ = prepared
    values[0].refresh_from_db()
    approved_at = values[0].target_approved_at
    future = timezone.now() + timedelta(minutes=31)
    with patch("apps.domains.dns_zone_recovery.timezone.now", return_value=future):
        fresh = inspect_cloudflare_zone_submission(actor=values[0].requested_by, change_id=values[0].pk, target_client=values[2])
        result = resolve(uncertain, observation_digest=fresh.observations[-1]["digest"])
    values[0].refresh_from_db()
    assert result.status == "complete" and values[0].target_approved_at == approved_at
    assert values[0].approved_record_plan.expires_at < future


def test_revoked_actor_during_resolution_cannot_publish_association(uncertain, django_user_model):
    values, _ = uncertain[0]
    original = values[2].list_zones.side_effect
    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        django_user_model.objects.filter(pk=values[0].requested_by_id).update(is_active=False)
        return result
    values[2].list_zones.side_effect = revoked
    result = resolve(uncertain)
    assert result.status == "review" and not result.provider_id and not result.receipt_digest
    assert DomainDNSReservation.objects.filter(change=values[0]).exists()


def test_zone_change_during_resolution_stays_in_review(uncertain):
    values, _ = uncertain[0]
    changed = zone(name=values[4].name, name_servers=["ns3.example.test", "ns4.example.test"])
    values[2].list_zones.side_effect = lambda *args, **kwargs: zone_page([changed])
    values[2].get_zone.side_effect = lambda *args, **kwargs: {"success": True, "result": changed}
    result = resolve(uncertain)
    assert result.status == "review" and not result.provider_id and not result.receipt_digest
    assert values[2].create_managed_zone.call_count == 1


def test_observed_records_are_retained_for_review_instead_of_overwritten(uncertain):
    values, _ = uncertain[0]
    values[2].list_dns_records.side_effect = lambda *args, **kwargs: record_page([row(name=values[4].name)])
    result = resolve(uncertain)
    values[0].refresh_from_db()
    assert result.receipt["record_review_required"] is True and values[0].status == "zone_review"


def test_foreign_actor_cannot_reconcile_but_owner_attribution_survives_customer_replay(uncertain, django_user_model):
    foreign = django_user_model.objects.create_user(email="foreign-zone-resolution@example.test", password="testpass")
    with pytest.raises(PermissionDenied):
        resolve(uncertain, actor=foreign)
    owner = django_user_model.objects.create_user(email="owner-zone-resolution@example.test", password="testpass", is_staff=True, is_superuser=True)
    assert resolve(uncertain, actor=owner).receipt["resolution"]["confirmed_by"] == owner.pk
    assert resolve(uncertain).receipt["resolution"]["confirmed_by"] == owner.pk


def test_reconciliation_audit_failure_keeps_unknown_marker_and_reservation(uncertain):
    create = AuditLog.objects.create
    def fail(*args, **kwargs):
        if kwargs.get("action") == "domain.dns.zone_resolution_confirmed":
            raise RuntimeError("audit unavailable")
        return create(*args, **kwargs)
    with patch("apps.domains.dns_zone_recovery.AuditLog.objects.create", side_effect=fail):
        with pytest.raises(RuntimeError):
            resolve(uncertain)
    step = DomainDNSExecutionStep.objects.get(pk=uncertain[1].pk)
    assert not step.receipt_digest and step.submitted_at and step.token
    assert DomainDNSReservation.objects.filter(change=uncertain[0][0][0]).exists()


def test_prepared_cancellation_keeps_zone_history_and_makes_fresh_request_possible(prepared_input):
    step = execute(prepared_input)
    values, _ = prepared_input
    count = values[2].list_zones.call_count
    with patch("requests.request", side_effect=AssertionError("Cancellation is local only")):
        result = cancel_prepared(prepared_input, step)
        assert cancel_prepared(prepared_input, step).cancelled_at == result.cancelled_at
    assert result.status == "cancelled" and result.zone_id == step.provider_id and result.zone_submitted_at == step.submitted_at
    assert values[2].list_zones.call_count == count and not DomainDNSReservation.objects.filter(change=result).exists()
    step.refresh_from_db()
    assert step.status == "complete" and step.receipt_digest
    fresh = create_dns_preview(actor=result.requested_by, domain_id=values[4].pk, target_provider="cloudflare", key=uuid.uuid4(),
        client=values[2], source_client=values[1])
    confirm(fresh, client=values[2], source_client=values[1])
    assert cancel_prepared(prepared_input, step).status == "cancelled"
    assert DomainDNSReservation.objects.get(domain=values[4]).change_id == fresh.pk


@pytest.mark.parametrize("changes", [{"confirmed": "true"}, {"keep_zone": False}, {"keep_zone": "true"}, {"receipt_digest": "wrong"}])
def test_prepared_cancellation_requires_explicit_retention_and_exact_receipt(prepared_input, changes):
    step = execute(prepared_input)
    with pytest.raises(ValidationError):
        cancel_prepared(prepared_input, step, **changes)
    assert DomainDNSReservation.objects.filter(change=prepared_input[0][0]).exists()


def test_unknown_submission_must_be_resolved_before_prepared_cancellation(uncertain):
    prepared, step = uncertain
    with pytest.raises(ValidationError, match="verified preparation"):
        cancel_prepared(prepared, step)
    assert DomainDNSReservation.objects.filter(change=prepared[0][0]).exists()


def test_explicitly_reconciled_preparation_can_be_cancelled_while_retaining_unknown_create_history(uncertain):
    result = resolve(uncertain)
    cancelled = cancel_prepared(uncertain[0], result)
    result.refresh_from_db()
    assert cancelled.status == "cancelled" and cancelled.zone_submitted_at == result.submitted_at
    assert result.receipt["provider_action_complete"] is False and result.receipt["zone_preparation_complete"] is True
    assert not DomainDNSReservation.objects.filter(change=cancelled).exists()


@pytest.mark.parametrize("downstream", ["submitted", "unknown", "acknowledged", "complete"])
def test_downstream_remote_or_unknown_work_prevents_prepared_cancellation(prepared_input, downstream):
    step = execute(prepared_input)
    body = {"stage": "record"}
    arguments = {}
    if downstream == "submitted":
        arguments["submitted_at"] = timezone.now()
    elif downstream == "unknown":
        arguments["status"] = "unknown"
    elif downstream == "acknowledged":
        arguments["provider_id"] = "remote-record"
    else:
        arguments["completed_at"] = timezone.now()
    DomainDNSExecutionStep.objects.create(change=prepared_input[0][0], stage="record", identity="mail", created_by=step.created_by,
        request=body, request_digest=_fingerprint(body), **arguments)
    with pytest.raises(ValidationError, match="Downstream"):
        cancel_prepared(prepared_input, step)
    assert DomainDNSReservation.objects.filter(change=prepared_input[0][0]).exists()


def test_prepared_cancellation_fences_read_only_downstream_work(prepared_input):
    step = execute(prepared_input)
    body = {"stage": "record"}
    other = DomainDNSExecutionStep.objects.create(change=prepared_input[0][0], stage="record", identity="mail", created_by=step.created_by,
        request=body, request_digest=_fingerprint(body), token=uuid.uuid4(), lease_until=timezone.now() + timedelta(minutes=5))
    cancel_prepared(prepared_input, step)
    other.refresh_from_db()
    assert other.status == "cancelled" and other.token is None and other.lease_until is None


def test_prepared_cancellation_audit_failure_rolls_back_local_state_and_slot(prepared_input):
    step = execute(prepared_input)
    with patch("apps.domains.dns_zone_recovery.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            cancel_prepared(prepared_input, step)
    prepared_input[0][0].refresh_from_db()
    assert prepared_input[0][0].cancelled_at is None and DomainDNSReservation.objects.filter(change=prepared_input[0][0]).exists()
