"""Durable registrar submission. An acknowledgement is never completion."""
import uuid
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from apps.admin_tools.secrets import IntegrationSecretError
from apps.audit.models import AuditLog
from .bulk_execution import locked_context, validate_admitted
from .bulk_reservations import release_item
from .contact_bindings import provider_scope
from .contact_change_confirmation import acknowledgement
from .models import BulkDomainItem
from .resellerclub_client import ResellerClubClient, ResellerClubError


@transaction.atomic
def _claim_remote(item_id, supplied_client, operation):
    owner, domain, batch, item = locked_context(item_id)
    if operation not in ("lock", "nameservers") or batch.action != operation:
        raise ValidationError("Choose the executor matching the admitted registrar operation.")
    if item.status != "queued" or item.submitted_at is not None or item.paused_at is not None:
        return None
    try:
        validate_admitted(owner, domain, batch, item)
        client = supplied_client if supplied_client is not None else ResellerClubClient()
        if provider_scope(client) != item.snapshot["provider_scope"]:
            raise ValidationError("The registrar account changed before submission.")
    except (ValidationError, IntegrationSecretError, ResellerClubError):
        item.status = "failed"
        item.message = "The saved request, domain or registrar configuration changed before submission. Review a new preview."
        item.save(update_fields=["status", "message", "updated_at"])
        release_item(item)
        AuditLog.objects.create(user=owner, action="domain.bulk.preflight_failed", model_name="BulkDomainItem", object_id=str(item.pk), data={"batch_id": batch.pk})
        return None
    item.status = "submitting"
    item.token = uuid.uuid4()
    item.submitted_at = timezone.now()
    item.message = "Registrar request admitted; completion has not been confirmed."
    item.save(update_fields=["status", "token", "submitted_at", "message", "updated_at"])
    AuditLog.objects.create(user=owner, action="domain.bulk.submitting", model_name="BulkDomainItem", object_id=str(item.pk), data={"batch_id": batch.pk, "operation": operation})
    return item.token, item.snapshot["registrar_id"], dict(batch.target), client


@transaction.atomic
def _record_remote_result(item_id, token, action_id):
    owner, domain, batch, item = locked_context(item_id)
    if item.token != token or item.status not in ("submitting", "unknown"):
        return item
    item.status = "awaiting" if action_id else "unknown"
    item.provider_action_id = action_id
    item.message = ("Registrar accepted the request. Waiting for independent state confirmation." if action_id
        else "The registrar outcome is uncertain. Review or reconcile this retained request; do not submit it again.")
    item.save(update_fields=["status", "provider_action_id", "message", "updated_at"])
    AuditLog.objects.create(user=owner, action="domain.bulk.submission_observed", model_name="BulkDomainItem", object_id=str(item.pk), data={"batch_id": batch.pk, "outcome": item.status})
    return item


def _execute_remote(item_id, operation, client):
    # A nested transaction would leave the marker uncommitted during provider HTTP.
    if connection.in_atomic_block:
        raise ValidationError("Remote domain execution must run outside an enclosing transaction.")
    admitted = _claim_remote(item_id, client, operation)
    if admitted is None:
        return BulkDomainItem.objects.get(pk=item_id)
    token, order_id, target, frozen_client = admitted
    action_id = ""
    try:
        if operation == "nameservers":
            result = frozen_client.modify_nameservers(order_id, target["nameservers"])
        else:
            result = frozen_client.lock_domain(order_id) if target["enabled"] else frozen_client.unlock_domain(order_id)
        action_id = acknowledgement(result, order_id=order_id).action_id
    except Exception:
        # Timeout, application error and malformed acknowledgement cannot prove no mutation.
        # Never persist response bodies, messages, exceptions or credentials.
        action_id = ""
    return _record_remote_result(item_id, token, action_id)


def execute_remote_lock(item_id, *, client=None):
    return _execute_remote(item_id, "lock", client)


def execute_remote_nameservers(item_id, *, client=None):
    return _execute_remote(item_id, "nameservers", client)
