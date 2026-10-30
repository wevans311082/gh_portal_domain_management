"""Selective fresh review of proven pre-submission failures, never a blind replay."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.crypto import constant_time_compare
from django.utils import timezone
from .models import BulkDomainBatch, BulkDomainItem, DomainContactChange, DomainOperationReservation


def retry_identity(owner, batch_id, item_ids):
    from .bulk_admission import _manifest
    if (type(batch_id) is not int or not 0 < batch_id <= 9223372036854775807
            or type(item_ids) is not list or not 1 <= len(item_ids) <= 100
            or any(type(pk) is not int or not 0 < pk <= 9223372036854775807 for pk in item_ids)
            or len(set(item_ids)) != len(item_ids)):
        raise ValidationError("Select between 1 and 100 distinct failed items for review.")
    source = BulkDomainBatch.objects.select_for_update().filter(pk=batch_id, user=owner).first()
    if source is None:
        raise PermissionDenied
    if source.confirmed_at is None or not constant_time_compare(source.manifest_digest, _manifest(source, list(source.items.all()))):
        raise ValidationError("The source batch requires review before retry.")
    rows = {item.pk: item for item in source.items.select_for_update().filter(pk__in=item_ids)}
    if len(rows) != len(item_ids):
        raise ValidationError("Every retry item must belong to the selected source batch.")
    return source, [rows[pk] for pk in item_ids]


def assert_retryable(items):
    for item in items:
        if (item.paused_at is not None or item.domain_id is None or item.selected_id != item.domain_id
                or item.status not in ("failed", "cancelled", "held") or item.submitted_at is not None
                or item.lease_until is not None and item.lease_until > timezone.now()
                or item.dispatch_until is not None and item.dispatch_until > timezone.now()
                or DomainOperationReservation.objects.filter(domain_id=item.domain_id).exists()):
            raise ValidationError("Retry only held or failed/cancelled items proven not to have been submitted. Completed, active and uncertain work stays retained.")
        if item.contact_change_id:
            from .bulk_contacts import _correlated
            child = DomainContactChange.objects.select_for_update().get(pk=item.contact_change_id)
            if (item.batch.action != "contacts" or not _correlated(item.batch, item, child)
                    or child.status not in ("failed", "cancelled") or child.submitted_at is not None):
                raise ValidationError("The linked contact request cannot be retried safely.")


@transaction.atomic
def create_retry_preview(*, customer, batch_id, item_ids, key, target=None):
    from .bulk_admission import _owner, create_bulk_preview
    owner = _owner(customer)
    source, items = retry_identity(owner, batch_id, item_ids)
    return create_bulk_preview(customer=owner, key=key, action=source.action,
        domain_ids=[item.selected_id for item in items], target=source.target if target is None else target,
        retry_of_id=source.pk, retry_item_ids=item_ids)
