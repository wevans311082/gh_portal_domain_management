"""Retained, explicitly approved source migration plans; no provider mutations."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.audit.models import AuditLog
from .contact_bindings import _fingerprint
from .dns_changes import _context, _manifest
from .dns_inventory import _validate
from .dns_record_plan import build_record_plan
from .models import DomainDNSChange, DomainDNSRecordPlan
from .resellerclub_client import ResellerClubClient


def _lock(actor, change_id):
    identity = DomainDNSChange.objects.filter(pk=change_id).values("domain_id").first()
    if identity is None:
        raise PermissionDenied
    actor, domain = _context(actor, identity["domain_id"])
    return actor, domain, DomainDNSChange.objects.select_for_update().get(pk=change_id)


def _intact(change):
    if (change.manifest_digest != _manifest(change) or not change.inventory_digest
            or change.inventory_digest != _fingerprint(change.inventory_receipt)
            or change.inventory_observed_at is None):
        raise ValidationError("The retained DNS inventory requires review.")


def _fresh(change, domain, source_client, target_client, registrar_client):
    _intact(change)
    if change.inventory_observed_at + timedelta(minutes=30) <= timezone.now():
        raise ValidationError("The DNS inventory expired. Obtain a fresh reviewed inventory.")
    if change.zone_submitted_at is not None:
        raise ValidationError("Provider work has started. Reconcile this request before changing its plan.")
    _validate(change, domain, source_client, target_client, registrar_client or ResellerClubClient())


@transaction.atomic
def create_record_plan(*, actor, change_id, ttl_overrides=None, source_client=None, target_client=None, registrar_client=None):
    actor, domain, change = _lock(actor, change_id)
    if change.status != "plan_review" or change.approved_record_plan_id is not None:
        raise ValidationError("This DNS request is not awaiting a new record plan.")
    _fresh(change, domain, source_client, target_client, registrar_client)
    body = build_record_plan(source_provider=change.source["dns_provider"], target_provider=change.target_provider,
        zone_name=change.source["name"], inventory_receipt=change.inventory_receipt,
        inventory_digest=change.inventory_digest, ttl_overrides=ttl_overrides)
    existing = DomainDNSRecordPlan.objects.filter(change=change, digest=body["digest"]).first()
    if existing:
        if existing.plan != body:
            raise ValidationError("The retained DNS record plan requires review.")
        return existing
    if change.record_plans.count() >= 20:
        raise ValidationError("This request has reached its record-plan revision limit. Review a new request.")
    plan = DomainDNSRecordPlan.objects.create(change=change, created_by=actor, plan=body, digest=body["digest"],
        expires_at=change.inventory_observed_at + timedelta(minutes=30))
    AuditLog.objects.create(user=actor, action="domain.dns.record_plan_created", model_name="DomainDNSRecordPlan",
        object_id=str(plan.pk), data={"change_id": change.pk, "entry_count": len(body["entries"]), "blocked": body["blocked"]})
    return plan


@transaction.atomic
def approve_record_plan(*, actor, plan_id, digest, confirmed, acknowledgements,
                        source_client=None, target_client=None, registrar_client=None):
    if confirmed is not True or type(acknowledgements) is not list or any(type(value) is not str for value in acknowledgements) or len(set(acknowledgements)) != len(acknowledgements):
        raise ValidationError("Explicitly confirm this record plan and its required acknowledgements.")
    identity = DomainDNSRecordPlan.objects.filter(pk=plan_id).values("change_id").first()
    if identity is None:
        raise PermissionDenied
    actor, domain, change = _lock(actor, identity["change_id"])
    plan = DomainDNSRecordPlan.objects.select_for_update().get(pk=plan_id)
    body = dict(plan.plan)
    recorded_digest = body.pop("digest", None)
    _intact(change)
    if (type(digest) is not str or not constant_time_compare(digest, plan.digest) or recorded_digest != plan.digest
            or _fingerprint(body) != plan.digest or body.get("inventory_digest") != change.inventory_digest
            or sorted(acknowledgements) != body.get("required_acknowledgements")):
        raise ValidationError("The reviewed DNS plan or its acknowledgements changed.")
    if plan.approved_at is not None:
        if change.approved_record_plan_id != plan.pk:
            raise ValidationError("The retained DNS plan approval requires review.")
        return plan
    if (change.status != "plan_review" or change.approved_record_plan_id is not None
            or plan.expires_at <= timezone.now()):
        raise ValidationError("This DNS plan is expired or no longer awaiting approval.")
    if body.get("blocked") is not False:
        raise ValidationError("Resolve every blocked DNS record before approving this migration plan.")
    _fresh(change, domain, source_client, target_client, registrar_client)
    plan.approved_at, plan.approved_by = timezone.now(), actor
    plan.save(update_fields=["approved_at", "approved_by", "updated_at"])
    change.approved_record_plan, change.status = plan, "plan_approved"
    change.save(update_fields=["approved_record_plan", "status", "updated_at"])
    AuditLog.objects.create(user=actor, action="domain.dns.record_plan_approved", model_name="DomainDNSRecordPlan",
        object_id=str(plan.pk), data={"change_id": change.pk, "acknowledgements": acknowledgements})
    return plan
