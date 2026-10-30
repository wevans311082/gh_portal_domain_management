"""Owner-only observations with current credentials for the original CF account."""
from dataclasses import asdict
from datetime import timedelta
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.admin_tools.access import is_owner
from apps.audit.models import AuditLog
from apps.cloudflare_integration.scope import provider_scope
from apps.cloudflare_integration.services import CloudflareError
from apps.cloudflare_integration.zones import find_zone
from .contact_bindings import _fingerprint
from .dns_plan_review import _lock
from .dns_zone_execution import _lease, _review
from .dns_zone_recovery import _binding, _step
from .models import DomainDNSRecoveryEvidence


def _owner_account(actor, change, step, client):
    if not is_owner(actor):
        raise PermissionDenied
    scope = provider_scope(client)
    if client.account_id != step.request["account_id"]:
        raise ValidationError("Recovery must use the original Cloudflare account. Account changes need a new request.")
    return scope


def inspect_cloudflare_zone_current_account(*, actor, change_id, target_client):
    """Read with replacement credentials, without rebinding any old consent.

    Owner evidence is append-only in separate rows so it does not replace or
    exhaust the bounded ordinary inspection history. No mutation is enabled.
    """
    if connection.in_atomic_block:
        raise ValidationError("Inspect credential recovery outside a database transaction.")
    with transaction.atomic():
        actor, domain, change = _lock(actor, change_id)
        if not is_owner(actor):
            raise PermissionDenied
        _binding(change, domain)
        step = _step(change)
        scope = _owner_account(actor, change, step, target_client)
        if (step.receipt_digest or change.cancelled_at is not None or change.completed_at is not None
                or step.submitted_at is None or step.submitted_at != change.zone_submitted_at
                or change.execution_steps.exclude(pk=step.pk).exists()):
            raise ValidationError("Only an isolated unresolved zone submission can use this recovery inspection.")
        if step.lease_until is not None and step.lease_until > timezone.now():
            raise ValidationError("A zone worker is still active. Wait for its retained outcome.")
        token = uuid.uuid4()
        step.status, step.token, step.lease_until = "observing", token, timezone.now() + timedelta(minutes=3)
        step.save(update_fields=["status", "token", "lease_until", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.dns.credential_inspection_started", model_name="DomainDNSExecutionStep",
            object_id=str(step.pk), data={"approved_provider_scope": change.provider_scope, "observed_provider_scope": scope,
                "account_id": step.request["account_id"], "writes_authorized": False})
    try:
        zone = find_zone(domain_name=change.source["name"], client=target_client, expected_scope=scope)
        observed_at = timezone.now()
        body = {"version": 1, "kind": "credential_recovery", "request_digest": step.request_digest,
            "approved_provider_scope": change.provider_scope, "observed_provider_scope": scope,
            "scope_changed": scope != change.provider_scope, "account_id": step.request["account_id"],
            "observed_by": actor.pk, "observed_at": observed_at.isoformat(), "zone": asdict(zone) if zone else None,
            "association_verified": False, "retry_authorized": False, "writes_authorized": False}
        with transaction.atomic():
            actor, domain, current = _lock(actor, change_id)
            _binding(current, domain)
            step = _step(current)
            _lease(step, token)
            if (_owner_account(actor, current, step, target_client) != scope or current.cancelled_at is not None
                    or current.execution_steps.exclude(pk=step.pk).exists()):
                raise ValidationError("The account or DNS request changed during credential recovery.")
            evidence = DomainDNSRecoveryEvidence.objects.create(step=step, created_by=actor, observed_at=observed_at,
                provider_scope=scope, body=body, digest=_fingerprint(body))
            step.status, step.token, step.lease_until = "review", None, None
            step.message = "Current-credential evidence retained for owner confirmation. No old consent or provider scope was changed."
            step.save(update_fields=["status", "token", "lease_until", "message", "updated_at"])
            current.status = "review"
            current.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.credential_evidence_observed", model_name="DomainDNSRecoveryEvidence",
                object_id=str(evidence.pk), data={"step_id": step.pk, "zone_found": zone is not None,
                    "approved_provider_scope": current.provider_scope, "observed_provider_scope": scope, "writes_authorized": False})
            return evidence
    except PermissionDenied:
        _review(change_id, token)
        raise
    except (ValidationError, CloudflareError):
        _review(change_id, token)
        raise ValidationError("The current-credential zone evidence could not be verified. Review the retained submission.") from None
