"""Retained post-preparation record decisions; approval never dispatches writes."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.audit.models import AuditLog
from .contact_bindings import _fingerprint
from .dns_apply_plan import build_apply_plan
from .dns_inventory import _validate
from .dns_plan_review import _lock
from .dns_target_review import target_evidence
from .dns_zone_recovery import _binding, _step
from .models import DomainDNSApplyPlan


def _evidence(change, domain):
    _binding(change, domain, change.target_provider)
    source, _ = target_evidence(change)
    step = _step(change)
    target = step.receipt.get("target")
    if (not step.receipt_digest or step.status != "complete" or step.completed_at is None
            or step.receipt.get("zone_preparation_complete") is not True
            or type(target) is not dict or target.get("exists") is not True
            or target.get("provider") != change.target_provider or target.get("scope") != change.provider_scope
            or type(target.get("zone")) is not dict or target["zone"].get("zone_id") != change.zone_id):
        raise ValidationError("Verify the prepared zone under its approved authority before reviewing record changes.")
    return source, step, target


def _no_record_work(change):
    if any(step.stage != "zone" and (step.submitted_at is not None or step.provider_id or step.receipt_digest
            or step.completed_at is not None or step.status not in {"preparing", "review", "cancelled"})
           for step in change.execution_steps.select_for_update()):
        raise ValidationError("Reconcile previously started DNS work before changing record consent.")


def _fresh(change, domain, source, step, source_client, target_client, registrar_client):
    if (change.cancelled_at is not None or change.completed_at is not None
            or change.status not in {"zone_ready", "zone_review", "record_review"}
            or source.expires_at <= timezone.now() or step.completed_at + timedelta(minutes=30) <= timezone.now()):
        raise ValidationError("This DNS record review is expired or no longer awaiting consent.")
    _no_record_work(change)
    _validate(change, domain, source_client, target_client, registrar_client)


def _body(change, source, step, target, decisions):
    return {"version": 1, "manifest_digest": change.manifest_digest, "source_plan_id": source.pk,
            "zone_step_id": step.pk, "zone_receipt_digest": step.receipt_digest,
            "records": build_apply_plan(source_plan=source.plan, target_receipt=target,
                target_digest=_fingerprint(target), decisions=decisions)}


@transaction.atomic
def create_apply_plan(*, actor, change_id, decisions=None, source_client, target_client, registrar_client):
    actor, domain, change = _lock(actor, change_id)
    source, step, target = _evidence(change, domain)
    _fresh(change, domain, source, step, source_client, target_client, registrar_client)
    if change.approved_apply_plan_id is not None:
        raise ValidationError("Retain the approved DNS application plan; reconcile it before any new intent.")
    body = _body(change, source, step, target, decisions)
    digest = _fingerprint(body)
    existing = DomainDNSApplyPlan.objects.filter(change=change, digest=digest).first()
    if existing:
        if _fingerprint(existing.plan) != digest:
            raise ValidationError("The retained DNS application plan requires review.")
        return existing
    if change.apply_plans.count() >= 20:
        raise ValidationError("This request reached its application-plan revision limit. Retain it for review.")
    plan = DomainDNSApplyPlan.objects.create(change=change, source_plan=source, zone_step=step,
        created_by=actor, plan=body, digest=digest,
        expires_at=min(source.expires_at, step.completed_at + timedelta(minutes=30)))
    change.status = "record_review"
    change.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(user=actor, action="domain.dns.apply_plan_created", model_name="DomainDNSApplyPlan",
        object_id=str(plan.pk), data={"change_id": change.pk, "digest": digest, "blocked": body["records"]["blocked"]})
    return plan


@transaction.atomic
def approve_apply_plan(*, actor, plan_id, digest, confirmed, acknowledgements,
                       source_client, target_client, registrar_client):
    if (confirmed is not True or type(acknowledgements) is not list
            or any(type(value) is not str for value in acknowledgements)
            or len(set(acknowledgements)) != len(acknowledgements)):
        raise ValidationError("Explicitly confirm this exact record plan and its acknowledgements.")
    identity = DomainDNSApplyPlan.objects.filter(pk=plan_id).values("change_id").first()
    if identity is None:
        raise PermissionDenied
    actor, domain, change = _lock(actor, identity["change_id"])
    source, step, target = _evidence(change, domain)
    plan = DomainDNSApplyPlan.objects.select_for_update().get(pk=plan_id)
    if (type(plan.plan) is not dict or type(plan.plan.get("records")) is not dict
            or type(digest) is not str or not constant_time_compare(digest, plan.digest)
            or plan.digest != _fingerprint(plan.plan) or plan.source_plan_id != source.pk or plan.zone_step_id != step.pk):
        raise ValidationError("The reviewed DNS application evidence changed.")
    rebuilt = _body(change, source, step, target, plan.plan["records"].get("decisions"))
    expected = rebuilt["records"]["required_acknowledgements"]
    if _fingerprint(rebuilt) != digest or sorted(acknowledgements) != expected:
        raise ValidationError("The exact record decisions or required acknowledgements changed.")
    if plan.approved_at is not None:
        if change.approved_apply_plan_id != plan.pk or plan.approved_by_id is None:
            raise ValidationError("The retained DNS application approval requires review.")
        return plan  # Historical consent only; do not renew freshness or dispatch.
    _fresh(change, domain, source, step, source_client, target_client, registrar_client)
    if (change.status != "record_review" or change.approved_apply_plan_id is not None
            or plan.expires_at <= timezone.now() or rebuilt["records"]["blocked"] is not False):
        raise ValidationError("Resolve blocked or expired record decisions before approving this plan.")
    plan.approved_at, plan.approved_by = timezone.now(), actor
    plan.save(update_fields=["approved_at", "approved_by", "updated_at"])
    change.approved_apply_plan, change.status = plan, "records_approved"
    change.save(update_fields=["approved_apply_plan", "status", "updated_at"])
    AuditLog.objects.create(user=actor, action="domain.dns.apply_plan_approved", model_name="DomainDNSApplyPlan",
        object_id=str(plan.pk), data={"change_id": change.pk, "digest": digest, "acknowledgements": expected})
    # A future worker must freshly re-observe source, target, registrar and DNSSEC
    # outside transactions, then journal each exact submitted mutation. Consent
    # to these records neither activates DNS nor authorizes delegation.
    return plan
