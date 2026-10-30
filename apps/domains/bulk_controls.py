"""Owner-attributed per-item scheduling controls; never cancel an admitted mutation."""
import uuid
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from apps.accounts.models import User
from apps.audit.models import AuditLog
from .bulk_admission import _manifest
from .bulk_execution import locked_context
from .models import DomainContactChange, DomainOperationReservation


@transaction.atomic
def set_bulk_paused(item_id, *, owner_id, paused, reason):
    """Trusted service; its eventual owner HTTP interface must require recent MFA."""
    if (type(paused) is not bool or type(reason) is not str or not 10 <= len(reason.strip()) <= 1000
            or any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in reason)):
        raise ValidationError("Choose pause or resume and supply a review reason of 10–1000 characters.")
    customer, domain, batch, item = locked_context(item_id)
    actor = User.objects.select_for_update().filter(pk=owner_id, is_active=True, is_staff=True, is_superuser=True).first()
    if (actor is None or batch.confirmed_at is None or item.status not in ("queued", "awaiting", "unknown", "submitting")
            or not constant_time_compare(batch.manifest_digest, _manifest(batch, list(batch.items.all())))
            or not DomainOperationReservation.objects.filter(domain=domain, item=item).exists()):
        raise ValidationError("An active owner must select an admitted, reserved bulk item.")
    if (item.paused_at is not None) == paused:
        return item
    item.paused_at = timezone.now() if paused else None
    item.paused_by = actor if paused else None
    item.pause_token = uuid.uuid4()
    fields = ["paused_at", "paused_by", "pause_token", "updated_at"]
    if not paused:
        item.available_at = item.dispatch_after = timezone.now()
        fields += ["available_at", "dispatch_after"]
    item.save(update_fields=fields)
    AuditLog.objects.create(user=actor, action="domain.bulk.paused" if paused else "domain.bulk.resumed", model_name="BulkDomainItem", object_id=str(item.pk), data={"batch_id": batch.pk, "reason": reason.strip()})
    if not paused:
        from .bulk_jobs import publish_bulk_item
        transaction.on_commit(lambda: publish_bulk_item(item.pk))
        if item.contact_change_id:
            child = DomainContactChange.objects.select_for_update().get(pk=item.contact_change_id)
            if child.status in ("queued", "awaiting", "unknown"):
                child.available_at = timezone.now()
                child.save(update_fields=["available_at", "updated_at"])
                from .contact_change_jobs import publish_contact_change
                transaction.on_commit(lambda: publish_contact_change(child.pk))
    return item
