"""Owner-attributed terminal-action review; never repeat a provider mutation."""
import re
import uuid
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from apps.accounts.models import User
from apps.audit.models import AuditLog
from .bulk_execution import locked_context, validate_admitted
from .bulk_observation import observed_lock, observed_nameservers
from .bulk_reservations import release_item
from .contact_bindings import provider_scope
from .contact_change_confirmation import ContactChangeAction, identifier

EVENT = "domain.bulk.lock_completion_reviewed"
NAMESERVER_EVENT = "domain.bulk.nameservers_completion_reviewed"


def _event(operation):
    return EVENT if operation == "lock" else NAMESERVER_EVENT


def _matches(operation, observed, target):
    if operation == "lock":
        return type(observed) is bool and observed == target
    return type(observed) is list and set(observed) == set(target)


def _mismatch(operation):
    return "The live registrar lock does not match the reviewed target." if operation == "lock" else "The live registrar nameservers do not match the reviewed target."


def _review_input(owner_id, action_id, action_type, reference, reason, success, stopped, operation):
    if (success is not True or stopped is not True or operation is not True or not identifier(action_id)
            or type(action_type) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", action_type)
            or type(reference) is not str or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", reference)
            or type(reason) is not str or not 10 <= len(reason.strip()) <= 1000
            or any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in reason)):
        raise ValidationError("Confirm the exact requested operation, provider success and stopped workers; supply the reviewed action, provider case and a reason of 10–1000 characters.")
    if not User.objects.filter(pk=owner_id, is_active=True, is_staff=True, is_superuser=True).exists():
        raise ValidationError("An active owner is required for terminal-action review.")


@transaction.atomic
def _prepare(item_id, owner_id, action_id, action_type, reference, reason, client, operation):
    customer, domain, batch, item = locked_context(item_id)
    actor = User.objects.select_for_update().filter(pk=owner_id, is_active=True, is_staff=True, is_superuser=True).first()
    if actor is None or operation not in ("lock", "nameservers") or batch.action != operation:
        raise ValidationError("An active owner must select the matching registrar-operation review.")
    if item.status == "completed" or operation == "nameservers" and item.registrar_completed_at is not None:
        receipt = AuditLog.objects.filter(action=_event(operation), model_name="BulkDomainItem", object_id=str(item.pk)).order_by("pk").first()
        if (receipt is None or receipt.user_id != owner_id or receipt.data.get("action_id") != action_id
                or receipt.data.get("action_type") != action_type or receipt.data.get("provider_reference") != reference
                or receipt.data.get("reason") != reason):
            raise ValidationError("Retain the original completion review; this request differs from its receipt.")
        return None
    now = timezone.now()
    if (item.paused_at is None or item.status not in ("awaiting", "unknown", "submitting") or item.submitted_at is None
            or item.submitted_at > now - timedelta(minutes=15) or item.updated_at > now - timedelta(minutes=15)
            or item.lease_until is not None and item.lease_until > now
            or item.dispatch_until is not None and item.dispatch_until > now
            or item.provider_action_id and item.provider_action_id != action_id):
        raise ValidationError("Stop/review workers and wait 15 minutes after the last update; use the exact retained provider action.")
    validate_admitted(customer, domain, batch, item, allow_lock_cache_change=True, allow_nameserver_cache_change=True, allow_paused=True)
    if provider_scope(client) != item.snapshot["provider_scope"]:
        raise ValidationError("The registrar account differs from the admitted request.")
    item.token = uuid.uuid4()  # Fence surviving callbacks and observations.
    item.lease_until = now + timedelta(minutes=5)
    item.save(update_fields=["token", "lease_until", "updated_at"])
    AuditLog.objects.create(user=actor, action="domain.bulk.completion_review_started", model_name="BulkDomainItem", object_id=str(item.pk), data={
        "batch_id": batch.pk, "action_id": action_id, "action_type": action_type,
        "provider_reference": reference, "reason": reason})
    return item.token, item.lease_until, dict(item.snapshot), item.submitted_at, batch.target["enabled"] if operation == "lock" else list(batch.target["nameservers"]), item.pause_token


def _action_matches(evidence, snapshot, action_id, action_type, submitted_at):
    now = timezone.now()
    if (not isinstance(evidence, ContactChangeAction) or evidence.order_id != snapshot["registrar_id"]
            or evidence.action_id != action_id or evidence.action_type != action_type
            or type(evidence.status) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", evidence.status)
            or not identifier(evidence.completed_at)
            or not int(submitted_at.timestamp()) <= int(evidence.completed_at) <= int(now.timestamp()) + 300):
        raise ValidationError("The exact reviewed provider action lacks matching terminal timestamp evidence.")


@transaction.atomic
def _finish(item_id, owner_id, token, lease, snapshot, action_id, action_type, evidence, observed, reference, reason, pause_token, operation):
    customer, domain, batch, item = locked_context(item_id)
    actor = User.objects.select_for_update().filter(pk=owner_id, is_active=True, is_staff=True, is_superuser=True).first()
    if (actor is None or item.paused_at is None or item.pause_token != pause_token
            or item.token != token or item.lease_until != lease or lease <= timezone.now()
            or item.status not in ("awaiting", "unknown", "submitting") or item.snapshot != snapshot):
        raise ValidationError("Owner access or the retained review changed during provider reads.")
    validate_admitted(customer, domain, batch, item, allow_lock_cache_change=True, allow_nameserver_cache_change=True, allow_paused=True)
    _action_matches(evidence, snapshot, action_id, action_type, item.submitted_at)
    target = batch.target["enabled"] if operation == "lock" else batch.target["nameservers"]
    if not _matches(operation, observed, target):
        raise ValidationError(_mismatch(operation))
    now = timezone.now()
    if operation == "lock":
        domain.is_locked = observed
        domain.save(update_fields=["is_locked", "updated_at"])
        item.observed_lock = observed
    else:
        from apps.services.models import Service
        domain.set_nameserver_cache(observed)
        domain.save(update_fields=["registrar_nameservers", "nameserver1", "nameserver2", "nameserver3", "nameserver4", "updated_at"])
        item.observed_nameservers = observed
        item.registrar_completed_at = now
        item.hosting_status = "pending" if Service.objects.filter(user=customer, domain_name__iexact=domain.name, status=Service.STATUS_ACTIVE).exists() else "not_required"
    item.status = "completed"
    item.completed_at = item.checked_at = now
    if operation == "nameservers" and item.hosting_status == "pending":
        item.status, item.completed_at = "awaiting", None
    item.provider_action_id = action_id
    item.lease_until = None
    item.message = ("The requested lock setting was confirmed after owner review of the exact completed provider action. Other transfer restrictions may still apply." if operation == "lock"
        else "Registrar delegation confirmed; hosting DNS follow-up still requires review." if item.hosting_status == "pending"
        else "Registrar delegation confirmed. No active hosting service requires follow-up.")
    item.save(update_fields=["status", "completed_at", "checked_at", "observed_lock", "observed_nameservers", "registrar_completed_at", "hosting_status", "provider_action_id", "lease_until", "message", "updated_at"])
    if item.status == "completed":
        release_item(item)
    AuditLog.objects.create(user=actor, action=_event(operation), model_name="BulkDomainItem", object_id=str(item.pk), data={
        "batch_id": batch.pk, "action_id": action_id, "action_type": action_type,
        "action_status_observed": evidence.status, "action_completed_at": evidence.completed_at,
        "owner_attested_provider_success": True, "owner_confirmed_workers_stopped": True,
        "owner_attested_exact_requested_operation": True,
        "live_target_matched": True, "hosting_followup_pending": item.hosting_status == "pending", "provider_reference": reference, "reason": reason})
    return item


def _confirm_completed(item_id, operation, *, owner_id, action_id, action_type, reference, reason,
                           provider_success_confirmed=False, workers_stopped_confirmed=False,
                           requested_operation_confirmed=False, client):
    """Service for a recent-MFA owner interface; no HTTP endpoint is enabled yet.

    Owner attestation supplies the unpublished action-success interpretation;
    exact action identity, chronology and matching live state remain mandatory.
    """
    if connection.in_atomic_block:
        raise ValidationError("Completion review must run outside an enclosing transaction.")
    _review_input(owner_id, action_id, action_type, reference, reason, provider_success_confirmed, workers_stopped_confirmed, requested_operation_confirmed)
    action_id, reason = identifier(action_id), reason.strip()
    claim = _prepare(item_id, owner_id, action_id, action_type, reference, reason, client, operation)
    if claim is None:
        from .models import BulkDomainItem
        return BulkDomainItem.objects.get(pk=item_id)
    token, lease, snapshot, submitted_at, target, pause_token = claim
    # Read-only calls occur after the fencing transaction commits. A failed review
    # retains its lease until expiry and never frees the uncertain domain slot.
    evidence = client.get_contact_change_action(snapshot["registrar_id"], action_id)
    _action_matches(evidence, snapshot, action_id, action_type, submitted_at)
    payload = client.get_registration_reconciliation_details(snapshot["registrar_id"])
    observed = observed_lock(payload, snapshot) if operation == "lock" else observed_nameservers(payload, snapshot)
    if provider_scope(client) != snapshot["provider_scope"]:
        raise ValidationError("The provider client changed during review.")
    if not _matches(operation, observed, target):
        raise ValidationError(_mismatch(operation))
    return _finish(item_id, owner_id, token, lease, snapshot, action_id, action_type, evidence, observed, reference, reason, pause_token, operation)


def confirm_lock_completed(item_id, **kwargs):
    return _confirm_completed(item_id, "lock", **kwargs)


def confirm_nameservers_registrar_completed(item_id, **kwargs):
    """Confirm only the registrar stage; hosted DNS follow-up is a separate outcome."""
    return _confirm_completed(item_id, "nameservers", **kwargs)
