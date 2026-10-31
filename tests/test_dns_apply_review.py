from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.contact_bindings import _fingerprint
from apps.domains.dns_apply_review import create_apply_plan, approve_apply_plan
from apps.domains.dns_zone_recovery import cancel_prepared_dns_change
from apps.domains.models import DomainDNSApplyPlan, DomainDNSChange, DomainDNSExecutionStep, DomainDNSReservation
from tests.test_whm_zone_execution import whm_ready, execute
from tests.test_dns_inventory_stage import admitted
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_zone_execution import prepared_input, execute as execute_cloudflare
from tests.test_whm_dns_inventory import rr

pytestmark = pytest.mark.django_db(transaction=True)


def create(whm_ready, **kwargs):
    values, _ = whm_ready
    change, source, target, registrar, domain = values
    return create_apply_plan(actor=change.requested_by, change_id=change.pk, source_client=source,
        target_client=target, registrar_client=registrar, **kwargs)


def approve(whm_ready, plan, **kwargs):
    change, source, target, registrar, domain = whm_ready[0]
    options = dict(actor=change.requested_by, plan_id=plan.pk, digest=plan.digest, confirmed=True,
        acknowledgements=plan.plan["records"]["required_acknowledgements"], source_client=source,
        target_client=target, registrar_client=registrar)
    options.update(kwargs)
    return approve_apply_plan(**options)


def test_prepared_zone_can_have_exact_record_decisions_reviewed_and_approved_without_http(whm_ready):
    step = execute(whm_ready)
    with patch("requests.request", side_effect=AssertionError("no HTTP")):
        result = create(whm_ready)
        assert create(whm_ready).pk == result.pk
        approved = approve(whm_ready, result)
        assert approve(whm_ready, result).approved_at == approved.approved_at
    assert approved.approved_by_id == whm_ready[0][0].requested_by_id
    change = DomainDNSChange.objects.get(pk=whm_ready[0][0].pk)
    assert change.status == "records_approved" and change.approved_apply_plan_id == result.pk
    assert result.zone_step_id == step.pk and result.source_plan_id == change.approved_record_plan_id
    assert DomainDNSExecutionStep.objects.filter(change=change).count() == 1
    assert whm_ready[0][4].dns_provider == "cloudflare" and DomainDNSReservation.objects.filter(change=change).exists()
    assert result.plan["records"]["delegation_authorized"] is False


def test_zone_must_be_prepared_before_review(whm_ready):
    with pytest.raises(ValidationError):
        create(whm_ready)
    assert not DomainDNSApplyPlan.objects.exists()


@pytest.mark.parametrize("field,value", [("confirmed", False), ("confirmed", 1), ("digest", "wrong"),
    ("acknowledgements", ["unreviewed"]), ("acknowledgements", [True]), ("acknowledgements", ["retain_target_only_records", "retain_target_only_records"])])
def test_exact_confirmation_digest_and_acknowledgements_required(whm_ready, field, value):
    execute(whm_ready)
    result = create(whm_ready)
    with pytest.raises(ValidationError):
        approve(whm_ready, result, **{field: value})
    result.refresh_from_db()
    assert result.approved_at is None


def test_unrelated_customer_cannot_review_or_approve(whm_ready, django_user_model):
    execute(whm_ready)
    result = create(whm_ready)
    stranger = django_user_model.objects.create_user(email="unrelated-apply@example.test", password="test")
    with pytest.raises(PermissionDenied):
        approve(whm_ready, result, actor=stranger)


def test_changed_provider_credentials_prevent_consent_but_replay_does_not_renew_it(whm_ready):
    execute(whm_ready)
    result = create(whm_ready)
    target = whm_ready[0][2]
    token = target.api_token
    target.api_token = "ROTATED"
    with pytest.raises(ValidationError):
        approve(whm_ready, result)
    target.api_token = token
    approved = approve(whm_ready, result)
    target.api_token = "ROTATED AGAIN"
    assert approve(whm_ready, result).approved_at == approved.approved_at


def test_source_or_plan_expiry_does_not_extend_existing_consent(whm_ready, monkeypatch):
    execute(whm_ready)
    result = create(whm_ready)
    future = timezone.now() + timedelta(hours=1)
    monkeypatch.setattr("apps.domains.dns_apply_review.timezone.now", lambda: future)
    with pytest.raises(ValidationError, match="expired"):
        approve(whm_ready, result)


def test_submitted_downstream_work_prevents_new_record_consent(whm_ready):
    execute(whm_ready)
    result = create(whm_ready)
    body = {"test": True}
    DomainDNSExecutionStep.objects.create(change=whm_ready[0][0], stage="records", identity="cpanel",
        created_by=whm_ready[0][0].requested_by, request=body, request_digest=_fingerprint(body), submitted_at=timezone.now())
    with pytest.raises(ValidationError, match="Reconcile"):
        approve(whm_ready, result)


def test_audit_failure_rolls_back_plan_creation_and_approval(whm_ready):
    execute(whm_ready)
    with patch("apps.domains.dns_apply_review.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            create(whm_ready)
    assert not DomainDNSApplyPlan.objects.exists()
    result = create(whm_ready)
    with patch("apps.domains.dns_apply_review.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            approve(whm_ready, result)
    result.refresh_from_db()
    change = DomainDNSChange.objects.get(pk=result.change_id)
    assert result.approved_at is None and change.approved_apply_plan_id is None and change.status == "record_review"


def test_record_consent_and_exact_plan_are_immutable(whm_ready):
    execute(whm_ready)
    result = approve(whm_ready, create(whm_ready))
    result.plan["records"]["final_records"] = []
    with pytest.raises(ValidationError, match="exact"):
        result.save()
    result.refresh_from_db()
    result.approved_at = timezone.now() + timedelta(minutes=1)
    with pytest.raises(ValidationError, match="original"):
        result.save()
    with pytest.raises(ValidationError, match="Retain"):
        result.delete()
    change = DomainDNSChange.objects.get(pk=result.change_id)
    change.approved_apply_plan_id = None
    with pytest.raises(ValidationError, match="association"):
        change.save()


def test_database_tampering_is_not_accepted_even_with_recomputed_outer_digest(whm_ready):
    execute(whm_ready)
    result = create(whm_ready)
    result.plan["records"]["final_records"] = []
    result.digest = _fingerprint(result.plan)
    DomainDNSApplyPlan.objects.filter(pk=result.pk).update(plan=result.plan, digest=result.digest)
    with pytest.raises(ValidationError, match="decisions"):
        approve(whm_ready, result)


@pytest.mark.parametrize("approved", [False, True])
def test_pending_or_approved_record_plan_can_be_cancelled_while_retaining_zone_and_consent(whm_ready, approved):
    step = execute(whm_ready)
    result = create(whm_ready)
    if approved:
        approve(whm_ready, result)
    change = DomainDNSChange.objects.get(pk=result.change_id)
    cancelled = cancel_prepared_dns_change(actor=change.requested_by, change_id=change.pk,
        manifest_digest=change.manifest_digest, receipt_digest=step.receipt_digest, confirmed=True,
        keep_zone=True, reason="user_cancelled")
    assert cancelled.status == "cancelled" and not DomainDNSReservation.objects.filter(change=change).exists()
    assert (DomainDNSApplyPlan.objects.get(pk=result.pk).approved_at is not None) is approved
    if approved:
        assert approve(whm_ready, result).approved_at is not None  # Historical only.
    else:
        with pytest.raises(ValidationError):
            approve(whm_ready, result)


def test_creation_and_consent_audit_events_bind_exact_plan(whm_ready):
    execute(whm_ready)
    result = approve(whm_ready, create(whm_ready))
    events = AuditLog.objects.filter(action__in=["domain.dns.apply_plan_created", "domain.dns.apply_plan_approved"], object_id=str(result.pk))
    assert events.count() == 2 and all(event.data["digest"] == result.digest for event in events)


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_generated_whm_conflicts_require_a_new_exact_revision_and_explicit_replacement_consent(whm_ready):
    target = whm_ready[0][2]
    original = target.parse_dns_zone.side_effect
    def generated(name):
        value = original(name)
        row = rr(50, "MX", ["0", name + "."], name + ".")
        row["ttl"] = 3600
        value["data"]["payload"].append(row)
        return value
    target.parse_dns_zone.side_effect = generated
    execute(whm_ready)
    blocked = create(whm_ready)
    assert blocked.plan["records"]["blocked"]
    with pytest.raises(ValidationError, match="blocked"):
        approve(whm_ready, blocked)
    changes = [dict(name=whm_ready[0][4].name + ".", type="MX", action="replace")]
    resolved = create(whm_ready, decisions=changes)
    assert resolved.pk != blocked.pk and not resolved.plan["records"]["blocked"]
    assert resolved.plan["records"]["required_acknowledgements"] == ["replace_target_records"]
    with pytest.raises(ValidationError, match="acknowledgements"):
        approve(whm_ready, resolved, acknowledgements=[])
    assert approve(whm_ready, resolved).approved_at is not None
    assert DomainDNSApplyPlan.objects.get(pk=blocked.pk).approved_at is None


def test_cloudflare_preparation_uses_the_same_record_review_service(prepared_input):
    execute_cloudflare(prepared_input)
    result = create(prepared_input)
    approved = approve(prepared_input, result)
    assert approved.plan["records"]["provider"] == "cloudflare" and approved.approved_at
