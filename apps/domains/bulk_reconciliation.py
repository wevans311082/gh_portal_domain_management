"""Read-only reconciliation of admitted lock mutations; never submit again."""
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from apps.admin_tools.secrets import IntegrationSecretError
from apps.audit.models import AuditLog
from .bulk_execution import locked_context, validate_admitted
from .bulk_observation import observed_lock, observed_nameservers
from .contact_bindings import provider_scope
from .models import BulkDomainItem
from .resellerclub_client import ResellerClubClient, ResellerClubError


@transaction.atomic
def _claim(item_id, supplied_client, operation):
    owner, domain, batch, item = locked_context(item_id)
    now = timezone.now()
    if operation not in ("lock", "nameservers") or batch.action != operation:
        raise ValidationError("Choose the observer matching the admitted registrar operation.")
    if (item.paused_at is not None or item.status not in ("awaiting", "unknown") or item.submitted_at is None
            or item.available_at > now or item.lease_until is not None and item.lease_until > now):
        return None
    try:
        validate_admitted(owner, domain, batch, item)
        client = supplied_client if supplied_client is not None else ResellerClubClient()
        if provider_scope(client) != item.snapshot["provider_scope"]:
            raise ValidationError("The registrar account changed.")
    except (ValidationError, IntegrationSecretError, ResellerClubError):
        item.status = "unknown"
        item.message = "The retained operation requires review before its registrar state can be matched."
        item.available_at = now + timedelta(minutes=5)
        item.lease_until = None
        item.save(update_fields=["status", "message", "available_at", "lease_until", "updated_at"])
        return None
    item.lease_until = now + timedelta(minutes=5)
    item.save(update_fields=["lease_until", "updated_at"])
    return item.token, item.lease_until, dict(item.snapshot), client


@transaction.atomic
def _record(item_id, token, lease, observed):
    owner, domain, batch, item = locked_context(item_id)
    if item.token != token or item.lease_until != lease or item.status not in ("awaiting", "unknown"):
        return item
    now = timezone.now()
    try:
        validate_admitted(owner, domain, batch, item, allow_paused=True)
    except (ValidationError, IntegrationSecretError):
        observed = None
    item.checked_at = now
    item.lease_until = None
    item.available_at = now + timedelta(minutes=5)
    if batch.action == "lock":
        item.observed_lock = observed if type(observed) is bool else None
        matched = type(observed) is bool and observed == batch.target["enabled"]
    else:
        item.observed_nameservers = observed if type(observed) is list else None
        matched = type(observed) is list and set(observed) == set(batch.target["nameservers"])
    if matched:
        item.message = "The registrar reports the requested setting. Action completion still requires confirmation; the domain remains reserved."
    elif observed is None:
        item.status = "unknown"
        item.message = "The registrar state could not be confirmed. The operation remains reserved for review; no mutation was repeated."
    else:
        item.message = "The registrar does not yet report the requested setting. No mutation was repeated."
    item.save(update_fields=["checked_at", "observed_lock", "observed_nameservers", "lease_until", "available_at", "status", "message", "updated_at"])
    AuditLog.objects.create(user=owner, action="domain.bulk." + ("lock" if batch.action == "lock" else "nameservers") + "_observed", model_name="BulkDomainItem", object_id=str(item.pk), data={"outcome": item.status, "batch_id": batch.pk})
    return item


def _observe_remote(item_id, operation, client):
    if connection.in_atomic_block:
        raise ValidationError("Registrar observation must run outside an enclosing transaction.")
    claim = _claim(item_id, client, operation)
    if claim is None:
        return BulkDomainItem.objects.get(pk=item_id)
    token, lease, snapshot, frozen_client = claim
    observed = None
    try:
        payload = frozen_client.get_registration_reconciliation_details(snapshot["registrar_id"])
        observed = observed_lock(payload, snapshot) if operation == "lock" else observed_nameservers(payload, snapshot)
    except Exception:
        pass  # Keep bodies, transport errors and private fields out of retained state.
    return _record(item_id, token, lease, observed)


def observe_remote_lock(item_id, *, client=None):
    return _observe_remote(item_id, "lock", client)


def observe_remote_nameservers(item_id, *, client=None):
    return _observe_remote(item_id, "nameservers", client)
