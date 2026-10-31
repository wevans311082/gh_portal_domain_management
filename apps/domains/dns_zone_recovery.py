"""Explicit zone-effect reconciliation and cancellation without remote cleanup."""
from datetime import datetime, timedelta
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.audit.models import AuditLog
from apps.admin_tools.access import is_owner
from apps.cloudflare_integration.scope import provider_scope
from apps.cloudflare_integration.services import CloudflareError
from .contact_bindings import _fingerprint
from .dns_controls import CANCELLATION_REASONS
from .dns_plan_review import _lock
from .dns_target_inventory import read_target_receipt
from .dns_target_review import target_evidence, validate_zone_consent
from .dns_zone_execution import _intact, _lease, _review, _assert_scope
from apps.provisioning.whm_client import WHMClientError
from .models import DomainDNSExecutionStep, DomainDNSReservation, DomainDNSRecoveryEvidence


def _binding(change, domain, provider="cloudflare"):
    _, comparison = target_evidence(change)
    validate_zone_consent(change, comparison)
    if (change.user_id != domain.user_id or change.source["name"] != domain.name
            or change.source["registrar_id"] != domain.registrar_id
            or provider not in {"cloudflare", "cpanel"} or change.target_provider != provider or change.target_approved_at is None
            or change.target_approved_by_id is None or change.target_approved_digest != change.target_digest):
        raise ValidationError("The retained DNS owner, source or target consent requires review.")


def _step(change):
    step = DomainDNSExecutionStep.objects.select_for_update().filter(change=change, stage="zone", identity=change.target_provider).first()
    if step is None:
        raise ValidationError("This DNS request has no retained zone execution evidence.")
    _intact(step, change)
    return step


def _selected_observation(*, step, change, actor, target_client, recovery_id):
    if recovery_id is None:
        if not step.observations:
            raise ValidationError("Inspect this submitted zone before reconciliation.")
        return step.observations[-1], change.provider_scope
    if not is_owner(actor):
        raise PermissionDenied
    evidence = DomainDNSRecoveryEvidence.objects.select_for_update().filter(pk=recovery_id, step=step).first()
    if evidence is None:
        raise PermissionDenied
    body = evidence.body
    current_scope = provider_scope(target_client)
    if (type(body) is not dict or evidence.digest != _fingerprint(body)
            or body.get("kind") != "credential_recovery" or body.get("request_digest") != step.request_digest
            or body.get("approved_provider_scope") != change.provider_scope
            or body.get("observed_provider_scope") != evidence.provider_scope or evidence.provider_scope != current_scope
            or body.get("account_id") != step.request["account_id"] or target_client.account_id != step.request["account_id"]
            or body.get("observed_by") != evidence.created_by_id or body.get("observed_at") != evidence.observed_at.isoformat()):
        raise ValidationError("The replacement-credential observation does not match this account and request.")
    return {**body, "digest": evidence.digest}, current_scope


def resolve_cloudflare_zone(*, actor, change_id, observation_digest, zone_id, confirmed, target_client):
    return _resolve_zone(actor=actor, change_id=change_id, observation_digest=observation_digest,
        zone_id=zone_id, confirmed=confirmed, target_client=target_client, recovery_id=None, provider="cloudflare")


def resolve_whm_zone(*, actor, change_id, observation_digest, zone_id, confirmed, target_client):
    return _resolve_zone(actor=actor, change_id=change_id, observation_digest=observation_digest,
        zone_id=zone_id, confirmed=confirmed, target_client=target_client, recovery_id=None, provider="cpanel")


def resolve_cloudflare_zone_after_credential_change(*, actor, change_id, evidence_id, evidence_digest, zone_id, confirmed, target_client):
    try:
        evidence_id = uuid.UUID(str(evidence_id))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Choose valid retained credential-recovery evidence.") from None
    return _resolve_zone(actor=actor, change_id=change_id, observation_digest=evidence_digest,
        zone_id=zone_id, confirmed=confirmed, target_client=target_client, recovery_id=evidence_id, provider="cloudflare")


def _resolve_zone(*, actor, change_id, observation_digest, zone_id, confirmed, target_client, recovery_id, provider):
    """Explicitly adopt an observed zone, without asserting who created it.

    This resolves the preparation effect, not the provenance of an uncertain
    create response. It never creates a zone, changes records or delegates DNS.
    Historical source approval is retained, never extended by reconciliation.
    """
    if confirmed is not True or type(observation_digest) is not str or type(zone_id) is not str:
        raise ValidationError("Explicitly confirm the exact observed zone before reconciliation.")
    if connection.in_atomic_block:
        raise ValidationError("Reconcile zone evidence outside a database transaction.")
    with transaction.atomic():
        actor, domain, change = _lock(actor, change_id)
        if recovery_id is not None and not is_owner(actor):
            raise PermissionDenied
        _binding(change, domain, provider)
        step = _step(change)
        if step.receipt_digest:
            resolution = step.receipt.get("resolution", {})
            if (resolution.get("observation_digest") != observation_digest or step.provider_id != zone_id
                    or recovery_id is not None and resolution.get("credential_recovery", {}).get("evidence_id") != str(recovery_id)):
                raise ValidationError("This zone request already has a different retained outcome.")
            return step
        if (change.cancelled_at is not None or change.completed_at is not None or step.submitted_at is None
                or step.submitted_at != change.zone_submitted_at
                or change.execution_steps.exclude(pk=step.pk).exists()):
            raise ValidationError("This zone request is not awaiting an isolated preparation reconciliation.")
        observation, observed_scope = _selected_observation(step=step, change=change, actor=actor,
            target_client=target_client, recovery_id=recovery_id)
        zone = observation.get("zone")
        if (not constant_time_compare(observation.get("digest", ""), observation_digest)
                or observation.get("request_digest") != step.request_digest or type(zone) is not dict
                or zone.get("zone_id") != zone_id or zone.get("provider_scope") != observed_scope
                or step.provider_id and step.provider_id != zone_id or change.zone_id and change.zone_id != zone_id):
            raise ValidationError("Review the latest matching zone observation before reconciliation.")
        observed_at = datetime.fromisoformat(observation["observed_at"])
        if observed_at > timezone.now():
            raise ValidationError("The zone observation time requires review.")
        # Historical candidates are usable only if the mandatory fresh read
        # below still matches exactly. This also permits recovery after the
        # bounded inspection history is full, without deleting that history.
        if step.lease_until is not None and step.lease_until > timezone.now():
            raise ValidationError("A zone worker is still active. Wait for its retained outcome.")
        _assert_scope(provider, observed_scope, target_client)
        token = uuid.uuid4()
        step.status, step.token, step.lease_until = "resolving", token, timezone.now() + timedelta(minutes=3)
        step.save(update_fields=["status", "token", "lease_until", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.dns.zone_resolution_started", model_name="DomainDNSExecutionStep",
            object_id=str(step.pk), data={"zone_id": zone_id, "observation_digest": observation_digest})
    try:
        target = read_target_receipt(provider=provider, name=change.source["name"], scope=observed_scope, client=target_client)
        if (not target["exists"] or target["zone"]["zone_id"] != zone_id
                or _fingerprint(target["zone"]) != _fingerprint(zone)):
            raise ValidationError("The observed zone changed. Inspect it again before reconciliation.")
        with transaction.atomic():
            actor, domain, current = _lock(actor, change_id)
            _binding(current, domain, provider)
            step = _step(current)
            _lease(step, token)
            selected, current_scope = _selected_observation(step=step, change=current, actor=actor,
                target_client=target_client, recovery_id=recovery_id)
            if (current.cancelled_at is not None or current.completed_at is not None
                    or selected["digest"] != observation_digest or current_scope != observed_scope
                    or current.execution_steps.exclude(pk=step.pk).exists()):
                raise ValidationError("The DNS request changed during reconciliation.")
            _assert_scope(provider, observed_scope, target_client)
            needs_review = bool(target["records"])
            receipt = {"version": 1, "mode": "reconciled_adopt", "target": target,
                "record_review_required": needs_review, "delegation_updated": False, "provider_action_complete": False,
                "zone_preparation_complete": True,
                "resolution": {"kind": "adopt_observed_zone", "original_submission_outcome": "unproven",
                    "observation_digest": observation_digest, "confirmed_by": actor.pk, "confirmed_at": timezone.now().isoformat()}}
            if recovery_id is not None:
                receipt["resolution"]["credential_recovery"] = {"evidence_id": str(recovery_id),
                    "approved_provider_scope": current.provider_scope, "observed_provider_scope": observed_scope,
                    "account_id": step.request["account_id"], "writes_authorized": False}
                receipt["new_execution_consent_required"] = True
            current.zone_id, current.zone_observation = zone_id, target["zone"]
            current.status = "zone_review" if needs_review or recovery_id is not None else "zone_ready"
            current.save(update_fields=["zone_id", "zone_observation", "status", "updated_at"])
            step.provider_id, step.status, step.completed_at = zone_id, "complete", timezone.now()
            step.receipt, step.receipt_digest = receipt, _fingerprint(receipt)
            step.token = step.lease_until = None
            step.message = "Observed zone explicitly adopted for preparation. Record and delegation stages remain pending."
            step.save(update_fields=["provider_id", "status", "completed_at", "receipt", "receipt_digest", "token",
                "lease_until", "message", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.zone_resolution_confirmed", model_name="DomainDNSExecutionStep",
                object_id=str(step.pk), data={"zone_id": zone_id, "observation_digest": observation_digest,
                    "resolution": "adopt_observed_zone", "original_submission_outcome": "unproven",
                    **({"credential_recovery": receipt["resolution"]["credential_recovery"]} if recovery_id is not None else {})})
            return step
    except (ValidationError, PermissionDenied, CloudflareError, WHMClientError):
        return _review(change_id, token)


@transaction.atomic
def cancel_prepared_dns_change(*, actor, change_id, manifest_digest, receipt_digest, confirmed, keep_zone, reason):
    """Stop pending automation after known preparation; never delete/roll back.

    A prepared zone is retained. Any submitted/known-complete downstream work,
    or unknown downstream state, requires its own reconciliation instead.
    """
    if (confirmed is not True or keep_zone is not True or type(reason) is not str or reason not in CANCELLATION_REASONS
            or type(manifest_digest) is not str or type(receipt_digest) is not str):
        raise ValidationError("Explicitly confirm cancellation and retention of the prepared zone.")
    actor, domain, change = _lock(actor, change_id)
    _binding(change, domain, change.target_provider)
    step = _step(change)
    prepared = step.receipt.get("zone_preparation_complete") is True or (
        step.receipt.get("mode") in {"create", "adopt"} and step.receipt.get("provider_action_complete") is True)
    if (not constant_time_compare(manifest_digest, change.manifest_digest) or not step.receipt_digest
            or not constant_time_compare(receipt_digest, step.receipt_digest)
            or not prepared
            or step.receipt.get("delegation_updated") is not False
            or step.receipt.get("target", {}).get("zone", {}).get("zone_id") != step.provider_id):
        raise ValidationError("Review the exact verified preparation receipt before cancellation.")
    if change.cancelled_at is not None:
        if (change.status != "cancelled" or change.cancellation_reason != reason
                or DomainDNSReservation.objects.filter(change=change).exists()):
            raise ValidationError("The retained DNS cancellation requires review.")
        return change
    if change.status not in {"zone_ready", "zone_review", "review", "record_review", "records_approved"} or change.completed_at is not None:
        raise ValidationError("This DNS request is not awaiting cancellation after preparation.")
    others = list(change.execution_steps.select_for_update().exclude(pk=step.pk).order_by("pk"))
    if any(other.submitted_at is not None or other.provider_id or other.receipt_digest or other.completed_at is not None
           or other.status not in {"preparing", "review", "cancelled"}
           or other.request_digest != _fingerprint(other.request) for other in others):
        raise ValidationError("Downstream DNS work may have started. Reconcile it before cancellation.")
    reservation = DomainDNSReservation.objects.select_for_update().filter(domain=domain).first()
    if reservation is None or reservation.change_id != change.pk:
        raise ValidationError("The DNS request's reservation requires review.")
    change.status, change.cancelled_at, change.cancelled_by = "cancelled", timezone.now(), actor
    change.cancellation_reason = reason
    change.inventory_token = change.inventory_lease_until = change.target_token = change.target_lease_until = None
    change.inventory_message = "Pending DNS changes cancelled. The prepared zone and existing remote settings were retained."
    change.save(update_fields=["status", "cancelled_at", "cancelled_by", "cancellation_reason", "inventory_token",
        "inventory_lease_until", "target_token", "target_lease_until", "inventory_message", "updated_at"])
    change.execution_steps.exclude(pk=step.pk).update(status="cancelled", token=None, lease_until=None)
    reservation.delete()
    AuditLog.objects.create(user=actor, action="domain.dns.prepared_cancelled", model_name="DomainDNSChange",
        object_id=str(change.pk), data={"zone_id": step.provider_id, "preparation_digest": receipt_digest, "keep_zone": True,
            "reason": reason, "remote_cleanup_performed": False})
    return change
