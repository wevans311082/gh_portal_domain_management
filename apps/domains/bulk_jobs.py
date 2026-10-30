"""Bounded durable-item dispatch. Feature-gated until the complete workflow is verified."""
import uuid
from datetime import timedelta
from time import monotonic
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from apps.audit.models import AuditLog
from apps.core.abuse import consume_limit, AbuseLimitExceeded, AbuseStoreUnavailable
from .bulk_execution import locked_context
from .models import BulkDomainItem


def execution_enabled():
    return getattr(settings, "DOMAIN_BULK_EXECUTION_ENABLED", False) is True


def publish_bulk_item(item_id):
    if not execution_enabled() or settings.CELERY_TASK_ALWAYS_EAGER:
        return False
    if not BulkDomainItem.objects.filter(pk=item_id, paused_at__isnull=True, batch__confirmed_at__isnull=False,
            status__in=("queued", "awaiting", "unknown")).exclude(batch__action="nameservers", registrar_completed_at__isnull=False).exists():
        return False
    try:
        run_bulk_item.delay(item_id)
    except Exception:
        return False
    return True


@transaction.atomic
def _claim(item_id):
    if type(item_id) is not int or not BulkDomainItem.objects.filter(pk=item_id, domain_id__isnull=False).exists():
        return None
    owner, domain, batch, item = locked_context(item_id)
    now = timezone.now()
    if batch.action == "nameservers" and item.registrar_completed_at is not None:
        return None
    if (item.paused_at is not None or batch.confirmed_at is None or item.status not in ("queued", "awaiting", "unknown")
            or item.available_at > now or item.dispatch_until is not None and item.dispatch_until > now
            or item.lease_until is not None and item.lease_until > now):
        return None
    try:
        consume_limit("bulk-domain-worker-customer", owner.pk, 10, 60)
        consume_limit("bulk-domain-worker", "global", 20, 60)
    except (AbuseLimitExceeded, AbuseStoreUnavailable) as exc:
        item.available_at = now + timedelta(seconds=getattr(exc, "retry_after", 60))
        item.save(update_fields=["available_at", "updated_at"])
        return None
    item.dispatch_token = uuid.uuid4()
    item.dispatch_until = now + timedelta(minutes=5)
    item.save(update_fields=["dispatch_token", "dispatch_until", "updated_at"])
    return item.dispatch_token, batch.action, item.status


@transaction.atomic
def _failed(item_id, dispatch_token):
    from .bulk_reservations import release_item
    owner, domain, batch, item = locked_context(item_id)
    if item.dispatch_token != dispatch_token or item.status in ("completed", "failed", "cancelled"):
        return
    if batch.action == "nameservers" and item.registrar_completed_at is not None:
        return
    if item.paused_at is not None and item.submitted_at is None and item.contact_change_id is None:
        return
    if item.submitted_at is not None or item.contact_change_id is not None:
        item.status = "unknown"
        item.message = "The retained operation requires review. No provider mutation will be repeated."
    else:
        item.status = "failed"
        item.message = "The operation stopped before submission. Review the affected item before trying again."
    item.save(update_fields=["status", "message", "updated_at"])
    if item.status == "failed":
        release_item(item)
    AuditLog.objects.create(user=owner, action="domain.bulk.worker_failed", model_name="BulkDomainItem", object_id=str(item.pk), data={"batch_id": batch.pk, "outcome": item.status})


@transaction.atomic
def _finish_dispatch(item_id, dispatch_token):
    owner, domain, batch, item = locked_context(item_id)
    if item.dispatch_token != dispatch_token:
        return
    item.dispatch_until = None
    item.available_at = max(item.available_at, timezone.now() + timedelta(seconds=60 if batch.action == "contacts" else 300))
    item.save(update_fields=["dispatch_until", "available_at", "updated_at"])


@shared_task(name="domains.run_bulk_item", ignore_result=True, soft_time_limit=210, time_limit=240)
def run_bulk_item(item_id):
    if not execution_enabled():
        return
    claim = _claim(item_id)
    if claim is None:
        return
    token, action, status = claim
    try:
        if action == "auto_renew":
            from .bulk_execution import execute_local_preference
            execute_local_preference(item_id)
        elif action == "contacts":
            from .bulk_contacts import execute_bulk_contact
            execute_bulk_contact(item_id)
        elif action in ("lock", "nameservers"):
            if status == "queued":
                from .bulk_remote import execute_remote_lock, execute_remote_nameservers
                (execute_remote_lock if action == "lock" else execute_remote_nameservers)(item_id)
            else:
                from .bulk_reconciliation import observe_remote_lock, observe_remote_nameservers
                (observe_remote_lock if action == "lock" else observe_remote_nameservers)(item_id)
        else:
            raise ValueError("Unsupported retained bulk operation")
    except Exception:
        _failed(item_id, token)
    finally:
        _finish_dispatch(item_id, token)


@transaction.atomic
def _quarantine_interrupted(item_id):
    owner, domain, batch, item = locked_context(item_id)
    now = timezone.now()
    if (item.paused_at is not None or batch.confirmed_at is None or item.status != "submitting" or item.submitted_at is None or item.submitted_at > now - timedelta(minutes=15)
            or item.dispatch_until is not None and item.dispatch_until > now
            or item.lease_until is not None and item.lease_until > now):
        return
    item.status = "unknown"
    item.message = "Submission was interrupted. Only observation or review may continue; no mutation will be repeated."
    item.save(update_fields=["status", "message", "updated_at"])
    AuditLog.objects.create(user=owner, action="domain.bulk.interrupted", model_name="BulkDomainItem", object_id=str(item.pk), data={"batch_id": batch.pk})


@shared_task(name="domains.dispatch_bulk_items", ignore_result=True, soft_time_limit=20, time_limit=30)
def dispatch_bulk_items():
    if not execution_enabled() or settings.CELERY_TASK_ALWAYS_EAGER:
        return 0
    now, deadline = timezone.now(), monotonic() + 8
    stale = BulkDomainItem.objects.filter(paused_at__isnull=True, batch__confirmed_at__isnull=False, domain_id__isnull=False,
        status="submitting", submitted_at__lte=now - timedelta(minutes=15)).filter(
        Q(dispatch_until__isnull=True) | Q(dispatch_until__lte=now)).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    for item_id in list(stale.order_by("pk").values_list("pk", flat=True)[:100]):
        if monotonic() >= deadline:
            return 0
        _quarantine_interrupted(item_id)
    due = BulkDomainItem.objects.filter(paused_at__isnull=True, batch__confirmed_at__isnull=False, domain_id__isnull=False,
        status__in=("queued", "awaiting", "unknown"), available_at__lte=now, dispatch_after__lte=now).filter(Q(dispatch_until__isnull=True) | Q(dispatch_until__lte=now))
    due = due.filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    due = due.exclude(batch__action="nameservers", registrar_completed_at__isnull=False)
    sent = 0
    for item_id in list(due.order_by("available_at", "pk").values_list("pk", flat=True)[:100]):
        if monotonic() >= deadline:
            break
        if not BulkDomainItem.objects.filter(pk=item_id, dispatch_after__lte=now).update(dispatch_after=now + timedelta(seconds=60)):
            continue
        # A failed or ambiguous broker acknowledgement keeps the same durable item.
        if publish_bulk_item(item_id):
            sent += 1
    return sent
