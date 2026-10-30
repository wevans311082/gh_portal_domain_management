"""Durable preview and exact confirmation, with no provider work or queue send."""
import uuid
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from apps.accounts.models import User
from apps.audit.models import AuditLog
from .bulk_preview import normalize_request, preview_bulk
from .contact_bindings import _fingerprint
from .models import BulkDomainBatch, BulkDomainItem, Domain

PREVIEW_LIFETIME = timedelta(minutes=30)


def _owner(customer):
    owner = User.objects.select_for_update().filter(pk=customer.pk, is_active=True, is_staff=False, is_superuser=False).first()
    if owner is None:
        raise PermissionDenied
    return owner


def _manifest(batch, items):
    values = {"user_id": batch.user_id, "action": batch.action, "target": batch.target,
        "request_digest": batch.request_digest, "expires_at": batch.expires_at.isoformat(),
        "items": [{"selected_id": item.selected_id, "domain_id": item.domain_id, "name": item.name,
            "status": item.preview_status, "snapshot": item.snapshot, "message": item.preview_message} for item in items]}
    if batch.retry_of_id is not None:
        values["retry"] = {"source_id": batch.retry_of_id, "item_ids": batch.retry_item_ids, "source_manifest": batch.retry_source_digest}
    return _fingerprint(values)


@transaction.atomic
def create_bulk_preview(*, customer, key, action, domain_ids, target, retry_of_id=None, retry_item_ids=None):
    action, domain_ids, target = normalize_request(action, domain_ids, target)
    try:
        key = uuid.UUID(str(key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Use a valid bulk request key.")
    owner = _owner(customer)
    identity_data = {"user_id": owner.pk, "action": action, "domain_ids": domain_ids, "target": target}
    source, original_items = None, []
    if retry_of_id is not None:
        from .bulk_retry import retry_identity
        source, original_items = retry_identity(owner, retry_of_id, retry_item_ids)
        if source.action != action or [item.selected_id for item in original_items] != domain_ids:
            raise ValidationError("Retry settings must match the selected retained items.")
        identity_data["retry"] = {"source_id": source.pk, "item_ids": retry_item_ids, "source_manifest": source.manifest_digest}
    elif retry_item_ids is not None:
        raise ValidationError("Retry items require their source batch.")
    identity = _fingerprint(identity_data)
    previous = BulkDomainBatch.objects.select_for_update().filter(key=key).first()
    if previous:
        if previous.user_id != owner.pk or not constant_time_compare(previous.request_digest, identity):
            raise ValidationError("This request key has already been used. Start a new preview.")
        if not constant_time_compare(previous.manifest_digest, _manifest(previous, list(previous.items.all()))):
            raise ValidationError("The retained preview requires review.")
        return previous
    if source is not None:
        from .bulk_retry import assert_retryable
        assert_retryable(original_items)
    preview = preview_bulk(customer=owner, action=action, domain_ids=domain_ids, target=target)
    batch = BulkDomainBatch(user=owner, key=key, action=action, target=target, request_digest=identity,
        expires_at=timezone.now() + PREVIEW_LIFETIME, retry_of=source, retry_item_ids=retry_item_ids or [],
        retry_source_digest=source.manifest_digest if source is not None else "")
    # Domain locks are held until all preview records and their audit commit.
    records = [BulkDomainItem(selected_id=item.domain_id, domain_id=item.domain_id if item.status != "unavailable" else None,
        name=item.name, preview_status=item.status, status=item.status, snapshot=item.snapshot,
        preview_message=item.message, message=item.message) for item in preview["items"]]
    batch.manifest_digest = _manifest(batch, records)
    batch.save()
    for item in records:
        item.batch = batch
    BulkDomainItem.objects.bulk_create(records)
    AuditLog.objects.create(user=owner, action="domain.bulk.previewed", model_name="BulkDomainBatch", object_id=str(batch.pk), data={"action": action, "item_count": len(records)})
    if source is not None:
        AuditLog.objects.create(user=owner, action="domain.bulk.retry_previewed", model_name="BulkDomainBatch", object_id=str(batch.pk), data={"source_batch_id": source.pk, "source_item_ids": retry_item_ids, "source_states": [item.status for item in original_items]})
    return batch


@transaction.atomic
def confirm_bulk(*, customer, batch_id, manifest_digest, confirmed):
    if confirmed is not True:
        raise ValidationError("Explicitly confirm the reviewed bulk changes.")
    owner = _owner(customer)
    candidate = BulkDomainBatch.objects.filter(pk=batch_id, user=owner).first()
    if candidate is None:
        raise PermissionDenied
    # Customer -> domains in stable order -> batch/items, as in preview admission.
    list(Domain.objects.select_for_update().filter(user=owner,
        pk__in=candidate.items.exclude(domain_id=None).values("domain_id")).order_by("pk"))
    batch = BulkDomainBatch.objects.select_for_update().get(pk=candidate.pk)
    items = list(batch.items.select_for_update().all())
    if (type(manifest_digest) is not str or not constant_time_compare(manifest_digest, batch.manifest_digest)
            or not constant_time_compare(batch.manifest_digest, _manifest(batch, items))):
        raise ValidationError("The reviewed bulk preview changed. Start a new preview.")
    if batch.confirmed_at:
        return batch  # Do not re-admit, requeue or reinterpret completed/uncertain work.
    if batch.expires_at <= timezone.now():
        raise ValidationError("This preview expired. Review a new preview before confirming.")
    if batch.retry_of_id is not None:
        from .bulk_retry import retry_identity, assert_retryable
        source, original_items = retry_identity(owner, batch.retry_of_id, batch.retry_item_ids)
        if source.manifest_digest != batch.retry_source_digest:
            raise ValidationError("The retained retry source changed.")
        assert_retryable(original_items)
    current = preview_bulk(customer=owner, action=batch.action, domain_ids=[item.selected_id for item in items], target=batch.target)
    for retained, latest in zip(items, current["items"], strict=True):
        if (retained.preview_status != latest.status or retained.snapshot != latest.snapshot
                or retained.name != latest.name or retained.preview_message != latest.message):
            raise ValidationError("A selected domain, contact or policy changed. Review a new preview.")
    eligible = [item for item in items if item.preview_status in ("ready", "unchanged")]
    if not eligible:
        raise ValidationError("No selected domains are ready. Review the held items first.")
    now = timezone.now()
    for item in eligible:
        item.status = "completed" if item.preview_status == "unchanged" else "queued"
        item.completed_at = now if item.status == "completed" else None
        item.message = "The local preference already matches." if item.status == "completed" else "Confirmed and waiting for execution."
        item.save(update_fields=["status", "completed_at", "message", "updated_at"])
        if item.status == "queued":
            from .bulk_reservations import reserve_item
            reserve_item(item)
    batch.confirmed_at = now
    batch.save(update_fields=["confirmed_at", "updated_at"])
    AuditLog.objects.create(user=owner, action="domain.bulk.confirmed", model_name="BulkDomainBatch", object_id=str(batch.pk), data={"eligible_count": len(eligible)})
    from .bulk_jobs import publish_bulk_item
    for item in eligible:
        if item.status == "queued":
            transaction.on_commit(lambda item_id=item.pk: publish_bulk_item(item_id))
    return batch
