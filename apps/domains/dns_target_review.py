"""Explicit consent for exact target evidence; does not dispatch provider writes."""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.audit.models import AuditLog
from apps.cloudflare_integration.services import CloudflareError
from .contact_bindings import _fingerprint
from .dns_plan_review import _lock, _intact, _fresh
from .dns_target_plan import build_target_plan
from .models import DomainDNSRecordPlan


DEFER_RECORD_CONFLICTS = "defer_record_conflicts_until_separate_approval"
RESOLVABLE_CONFLICTS = frozenset({"rrset_answers_differ", "ttl_differs", "provider_behaviour_differs", "cname_conflict"})


def required_zone_acknowledgements(comparison, *, defer_conflicts=False):
    """Exact consent to zone preparation, without approving record changes."""
    expected = list(comparison["required_acknowledgements"])
    if defer_conflicts:
        blocked = [action for action in comparison["actions"] if action["action"] == "blocked"]
        if (comparison["blocked"] is not True or comparison["target_exists"] is not True
                or comparison["target_plan"]["blocked"] is not False or not blocked
                or any(action["reason"] not in RESOLVABLE_CONFLICTS for action in blocked)):
            raise ValidationError("Unsupported or inconsistent target records cannot be deferred for zone adoption.")
        expected.append(DEFER_RECORD_CONFLICTS)
    return sorted(expected)


def validate_zone_consent(change, comparison):
    values = change.target_acknowledgements
    if type(values) is not list or any(type(value) is not str for value in values):
        raise ValidationError("The retained zone-preparation consent requires review.")
    deferred = DEFER_RECORD_CONFLICTS in values
    expected = required_zone_acknowledgements(comparison, defer_conflicts=deferred)
    if values != expected or (comparison["blocked"] is not False and not deferred):
        raise ValidationError("Target record conflicts require separate explicit zone-preparation consent.")


def target_evidence(change):
    """Validate retained evidence without treating old approval as live consent.

    Caller holds customer/domain/request locks. Historical replay deliberately
    does not refresh expiry or consult providers. A worker must additionally
    verify freshness and current remote state before every mutation stage.
    """
    _intact(change)
    if (type(change.target_receipt) is not dict or not change.target_digest or change.target_digest != _fingerprint(change.target_receipt)
            or change.target_observed_at is None):
        raise ValidationError("The retained target DNS evidence requires review.")
    plan = DomainDNSRecordPlan.objects.select_for_update().filter(pk=change.approved_record_plan_id, change=change).first()
    if plan is None or plan.approved_at is None or plan.approved_by_id is None or type(plan.plan) is not dict:
        raise ValidationError("The retained source DNS approval requires review.")
    body = dict(plan.plan)
    digest = body.pop("digest", None)
    if (digest != plan.digest or _fingerprint(body) != plan.digest
            or body.get("inventory_digest") != change.inventory_digest or body.get("blocked") is not False):
        raise ValidationError("The retained source DNS plan requires review.")
    receipt = change.target_receipt
    target = receipt.get("target")
    if (type(receipt.get("version")) is not int or receipt["version"] != 1 or receipt.get("source_plan_id") != plan.pk
            or receipt.get("source_plan_digest") != plan.digest or type(target) is not dict
            or target.get("scope") != change.provider_scope or target.get("provider") != change.target_provider
            or body.get("target_provider") != change.target_provider):
        raise ValidationError("The target DNS evidence is not bound to this request and source plan.")
    rebuilt = build_target_plan(source_plan=plan.plan, target_receipt=target, target_digest=_fingerprint(target))
    if rebuilt != receipt.get("comparison"):
        raise ValidationError("The retained target DNS comparison requires a new review.")
    return plan, rebuilt


@transaction.atomic
def approve_target_plan(*, actor, change_id, digest, confirmed, acknowledgements,
                        source_client=None, target_client=None, registrar_client=None):
    if (confirmed is not True or type(acknowledgements) is not list
            or any(type(value) is not str for value in acknowledgements)
            or len(set(acknowledgements)) != len(acknowledgements)):
        raise ValidationError("Explicitly confirm the target DNS plan and its required acknowledgements.")
    actor, domain, change = _lock(actor, change_id)
    plan, comparison = target_evidence(change)
    deferred = DEFER_RECORD_CONFLICTS in acknowledgements
    expected = required_zone_acknowledgements(comparison, defer_conflicts=deferred)
    if (type(digest) is not str or not constant_time_compare(digest, change.target_digest)
            or sorted(acknowledgements) != expected):
        raise ValidationError("The reviewed target DNS plan or its acknowledgements changed.")
    if change.target_approved_at is not None:
        if (change.target_approved_by_id is None or change.target_approved_digest != digest
                or change.target_acknowledgements != expected):
            raise ValidationError("The retained target DNS approval requires review.")
        return change
    if (change.status != "target_review" or change.cancelled_at is not None
            or plan.expires_at <= timezone.now()
            or change.target_observed_at + timedelta(minutes=30) <= timezone.now()):
        raise ValidationError("This target DNS plan is expired or no longer awaiting approval.")
    if comparison["blocked"] is not False and not deferred:
        raise ValidationError("Resolve every target DNS conflict before approving this plan.")
    if change.target_provider == "cpanel":
        from .dns_changes import configured_whm_nameservers, zone_creation_options
        from .registry_requirements import validate_nameservers
        validate_nameservers(domain.name, list(configured_whm_nameservers(target_client)))
        if not change.target_receipt["target"]["exists"] and (
                not change.zone_creation or zone_creation_options("cpanel", target_client) != change.zone_creation):
            raise ValidationError("Review explicit WHM creation settings before approving a missing target zone.")
    try:
        _fresh(change, domain, source_client, target_client, registrar_client)
    except CloudflareError:
        raise ValidationError("The DNS provider configuration changed. Review a new request.") from None
    if change.zone_id or change.zone_observation or change.completed_at is not None:
        raise ValidationError("Provider work needs reconciliation before new target approval.")
    change.target_approved_at, change.target_approved_by = timezone.now(), actor
    change.target_approved_digest, change.target_acknowledgements = digest, expected
    change.status = "target_approved"
    change.save(update_fields=["target_approved_at", "target_approved_by", "target_approved_digest",
        "target_acknowledgements", "status", "updated_at"])
    AuditLog.objects.create(user=actor, action="domain.dns.target_approved", model_name="DomainDNSChange",
        object_id=str(change.pk), data={"source_plan_id": plan.pk, "target_digest": digest, "acknowledgements": expected,
            "scope": "zone_preparation_only", "record_conflicts_deferred": deferred})
    # No provider request or queue publication: execution must re-observe both
    # remote snapshots, DNSSEC and delegation under durable submission fences.
    return change
