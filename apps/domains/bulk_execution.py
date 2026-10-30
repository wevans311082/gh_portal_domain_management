"""Atomic local-preference execution; remote mutations require separate fences."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.admin_tools.secrets import IntegrationSecretError
from .bulk_admission import _manifest
from .bulk_preview import _snapshot
from .bulk_reservations import release_item, BulkOperationPaused
from .contact_bindings import _fingerprint
from .models import BulkDomainBatch, BulkDomainItem, Domain, DomainOperationReservation


def locked_context(item_id):
    """Caller owns an atomic block; retain the shared customer/domain lock order."""
    identity = BulkDomainItem.objects.values("batch_id", "batch__user_id", "domain_id").get(pk=item_id)
    owner = User.objects.select_for_update().get(pk=identity["batch__user_id"])
    domain = Domain.objects.select_for_update().get(pk=identity["domain_id"])
    batch = BulkDomainBatch.objects.select_for_update().get(pk=identity["batch_id"])
    item = BulkDomainItem.objects.select_for_update().get(pk=item_id)
    return owner, domain, batch, item


def validate_admitted(owner, domain, batch, item, *, allow_lock_cache_change=False, allow_nameserver_cache_change=False, allow_paused=False):
    if item.paused_at is not None and not allow_paused:
        raise BulkOperationPaused("This bulk item is paused for review.")
    if (not owner.is_active or owner.is_staff or owner.is_superuser or domain.user_id != owner.pk
            or batch.confirmed_at is None
            or not constant_time_compare(batch.manifest_digest, _manifest(batch, list(batch.items.all())))
            or not DomainOperationReservation.objects.filter(domain=domain, item=item).exists()):
        raise ValidationError("The admitted operation requires review.")
    snapshot = _snapshot(domain, batch.action, batch.target, bulk_item_id=item.pk)
    if allow_lock_cache_change and batch.action == "lock":
        # Terminal owner review independently verifies live state. A refreshed
        # local cache must not make an otherwise exact request unrecoverable.
        snapshot["is_locked"] = item.snapshot.get("is_locked")
    if allow_nameserver_cache_change and batch.action == "nameservers":
        snapshot["previous_nameservers"] = item.snapshot.get("previous_nameservers")
    snapshot.update(action=batch.action, target=batch.target)
    snapshot["digest"] = _fingerprint(snapshot)
    if snapshot != item.snapshot:
        raise ValidationError("The admitted subject changed.")


@transaction.atomic
def execute_local_preference(item_id):
    owner, domain, batch, item = locked_context(item_id)
    if batch.action != "auto_renew":
        raise ValidationError("This executor handles only local auto-renew preferences.")
    if item.status != "queued" or item.submitted_at is not None or item.paused_at is not None:
        return item
    try:
        validate_admitted(owner, domain, batch, item)
    except (ValidationError, IntegrationSecretError):
        item.status = "failed"
        item.message = "The customer, domain, policy or saved request changed before execution. Review a new preview."
        item.save(update_fields=["status", "message", "updated_at"])
        release_item(item)
        AuditLog.objects.create(user=owner, action="domain.bulk.preflight_failed", model_name="BulkDomainItem", object_id=str(item.pk), data={"batch_id": batch.pk})
        return item
    domain.auto_renew = batch.target["enabled"]
    domain.save(update_fields=["auto_renew", "updated_at"])
    item.status = "completed"
    item.completed_at = timezone.now()
    item.message = "Auto-renew preference saved. Future payment and registrar renewal are not guaranteed."
    item.save(update_fields=["status", "completed_at", "message", "updated_at"])
    release_item(item)
    AuditLog.objects.create(user=owner, action="domain.bulk.preference_completed", model_name="BulkDomainItem", object_id=str(item.pk), data={"batch_id": batch.pk})
    return item
