"""Atomic linkage to the existing contact-change pipeline; never mutate twice."""
import uuid
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from apps.admin_tools.secrets import IntegrationSecretError
from apps.audit.models import AuditLog
from .bulk_admission import _manifest
from .bulk_execution import locked_context, validate_admitted
from .bulk_reservations import release_item, BulkOperationPaused
from .models import BulkDomainItem, DomainContactChange, DomainOperationReservation


def child_key(batch, item):
    return uuid.uuid5(batch.key, "contact-change:" + str(item.pk))


def _correlated(batch, item, child):
    return (child.key == child_key(batch, item) and child.domain_id == item.domain_id
        and child.user_id == batch.user_id and child.domain_name == item.name
        and child.registrar_order_id == item.snapshot.get("registrar_id")
        and child.provider_scope == item.snapshot.get("provider_scope")
        and child.customer_id == item.snapshot.get("customer_id")
        and child.subject.get("roles") == batch.target["contacts"]
        and child.subject.get("contact_digests") == item.snapshot.get("contact_digests")
        and child.subject.get("publication_choice") == batch.target["publication_choice"]
        and child.subject.get("source_id") == item.snapshot.get("source_id")
        and child.subject.get("source_kind") == item.snapshot.get("source_kind")
        and child.subject.get("previous_remote_ids") == item.snapshot.get("remote_contacts")
        and child.subject.get("previous_roles") == item.snapshot.get("previous_roles"))


def admitted_parent(owner, domain, item_id, key, contacts, publication_choice):
    """Called inside contact admission's durable queue/customer/domain transaction."""
    if type(item_id) is not int or not BulkDomainItem.objects.filter(pk=item_id, domain=domain, batch__user=owner).exists():
        raise PermissionDenied
    customer, locked_domain, batch, item = locked_context(item_id)
    if item.paused_at is not None:
        raise BulkOperationPaused("This bulk contact item is paused for review.")
    if (batch.action != "contacts" or customer.pk != owner.pk or locked_domain.pk != domain.pk
            or batch.confirmed_at is None or key != child_key(batch, item)
            or contacts != batch.target["contacts"] or publication_choice != batch.target["publication_choice"]
            or not constant_time_compare(batch.manifest_digest, _manifest(batch, list(batch.items.all())))):
        raise ValidationError("The contact request does not match its confirmed bulk item.")
    if item.contact_change_id:
        child = DomainContactChange.objects.select_for_update().get(pk=item.contact_change_id)
        if not _correlated(batch, item, child):
            raise ValidationError("The retained contact request requires review.")
        return item, child
    if item.status != "queued" or item.submitted_at is not None:
        raise ValidationError("This bulk item cannot admit another contact request.")
    validate_admitted(customer, locked_domain, batch, item)
    return item, None


@transaction.atomic
def sync_bulk_contact(item_id):
    owner, domain, batch, item = locked_context(item_id)
    if batch.action != "contacts":
        raise ValidationError("Choose a bulk contact-change item.")
    if item.paused_at is not None or not item.contact_change_id:
        return item
    child = DomainContactChange.objects.select_for_update().get(pk=item.contact_change_id)
    if (not constant_time_compare(batch.manifest_digest, _manifest(batch, list(batch.items.all())))
            or not _correlated(batch, item, child)):
        raise ValidationError("The bulk/contact association requires review.")
    if (item.status == "completed" and child.status == "confirmed" and child.confirmed_at is not None
            or item.status in ("failed", "cancelled") and child.status in ("failed", "cancelled") and child.submitted_at is None):
        return item
    if not DomainOperationReservation.objects.filter(domain=domain, item=item).exists():
        raise ValidationError("The bulk/contact reservation requires review.")
    if child.status == "confirmed" and child.confirmed_at is not None:
        status, message = "completed", "The linked contact change has been confirmed."
    elif child.status in ("failed", "cancelled") and child.submitted_at is None:
        status, message = child.status, "The linked contact change stopped before submission. Review the affected item before trying again."
    elif child.status in ("unknown", "submitting", "failed", "cancelled", "confirmed"):
        status, message = "unknown", "The linked contact outcome requires review. No replacement contact request will be submitted."
    else:
        status, message = "awaiting", "The linked contact change is processing or awaiting applicable registrant approval."
    if (item.status == status and item.message == message and item.submitted_at == child.submitted_at
            and item.provider_action_id == child.action_id):
        return item
    item.status, item.message = status, message
    item.submitted_at = child.submitted_at
    item.provider_action_id = child.action_id
    item.completed_at = child.confirmed_at if status == "completed" else None
    item.checked_at = timezone.now()
    item.save(update_fields=["status", "message", "submitted_at", "provider_action_id", "completed_at", "checked_at", "updated_at"])
    if status in ("completed", "failed", "cancelled"):
        release_item(item)
    AuditLog.objects.create(user=owner, action="domain.bulk.contact_progress", model_name="BulkDomainItem", object_id=str(item.pk), data={"batch_id": batch.pk, "contact_change_id": child.pk, "outcome": status})
    return item


def validate_contact_link(child, *, allow_paused=False):
    """Recheck a linked parent at every existing worker preparation/submission gate."""
    item_id = BulkDomainItem.objects.filter(contact_change_id=child.pk).values_list("pk", flat=True).first()
    if item_id is None:
        return
    owner, domain, batch, item = locked_context(item_id)
    if item.paused_at is not None and not allow_paused:
        raise BulkOperationPaused("This bulk contact item is paused for review.")
    if (batch.action != "contacts" or batch.confirmed_at is None or item.status not in ("awaiting", "unknown")
            or not constant_time_compare(batch.manifest_digest, _manifest(batch, list(batch.items.all())))
            or not _correlated(batch, item, child)
            or not DomainOperationReservation.objects.filter(domain=domain, item=item).exists()):
        raise ValidationError("The linked bulk contact request is no longer admitted for provider work.")


@transaction.atomic
def _preflight_failed(item_id):
    owner, domain, batch, item = locked_context(item_id)
    if item.contact_change_id or item.status != "queued" or item.submitted_at is not None:
        return item
    item.status = "failed"
    item.message = "The contact proposal changed or is unavailable before admission. Review a new preview."
    item.save(update_fields=["status", "message", "updated_at"])
    release_item(item)
    AuditLog.objects.create(user=owner, action="domain.bulk.preflight_failed", model_name="BulkDomainItem", object_id=str(item.pk), data={"batch_id": batch.pk})
    return item


def execute_bulk_contact(item_id):
    if connection.in_atomic_block:
        raise ValidationError("Bulk contact admission must run outside an enclosing transaction.")
    item = BulkDomainItem.objects.select_related("batch__user").get(pk=item_id)
    batch = item.batch
    if batch.action != "contacts":
        raise ValidationError("Choose the contact-change executor for a contact batch.")
    if item.paused_at is not None:
        return item
    if item.contact_change_id:
        return sync_bulk_contact(item_id)
    if item.status != "queued":
        return item
    from .contact_changes import request_contact_change
    try:
        request_contact_change(customer=batch.user, domain_id=item.domain_id, key=child_key(batch, item),
            contacts=batch.target["contacts"], publication_choice=batch.target["publication_choice"], bulk_item_id=item.pk)
    except BulkOperationPaused:
        return BulkDomainItem.objects.get(pk=item_id)
    except (ValidationError, PermissionDenied, IntegrationSecretError):
        return _preflight_failed(item_id)
    return sync_bulk_contact(item_id)
