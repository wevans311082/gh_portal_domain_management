"""Bounded background submission and read-only observation of durable requests."""

from datetime import timedelta
from time import monotonic

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.domains.models import DomainContactChange


def publish_contact_change(identifier):
    # Customer HTTP requests must never run provider work under eager settings.
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return False
    if DomainContactChange.objects.filter(pk=identifier, bulk_item__paused_at__isnull=False).exists():
        return False
    try:
        run_contact_change.delay(identifier)
    except Exception:
        return False  # The durable row is redispatched later.
    return True


@shared_task(name="domains.run_contact_change", ignore_result=True, soft_time_limit=210, time_limit=240)
def run_contact_change(identifier):
    from apps.core.abuse import consume_limit, AbuseLimitExceeded, AbuseStoreUnavailable
    from apps.domains.contact_changes import submit_contact_change, observe_contact_change
    from apps.domains.resellerclub_client import ResellerClubClient

    with transaction.atomic():
        job = DomainContactChange.objects.select_for_update().get(pk=identifier)
        if DomainContactChange.objects.filter(pk=identifier, bulk_item__paused_at__isnull=False).exists():
            return
        now = timezone.now()
        if (job.available_at > now or job.lease_until is not None and job.lease_until > now
                or job.status not in {"queued", "awaiting", "unknown"}
                or job.status == "unknown" and not job.action_id):
            return
        try:
            consume_limit("registrar-contact-change-worker", "global", 20, 60)
        except (AbuseLimitExceeded, AbuseStoreUnavailable) as exc:
            job.available_at = now + timedelta(seconds=getattr(exc, "retry_after", 60))
            job.save(update_fields=["available_at", "updated_at"])
            return
        job.lease_until = now + timedelta(minutes=5)
        job.save(update_fields=["lease_until", "updated_at"])
        token = job.token
        operation = "submit" if job.status == "queued" else "observe"
    try:
        client = ResellerClubClient()
        if operation == "submit":
            submit_contact_change(identifier, client=client)
        else:
            observe_contact_change(identifier, client=client)
    except Exception as exc:
        # Validation/config failures before preparation cannot loop indefinitely.
        # Once a mutation marker exists, only explicit reconciliation may clear it.
        if operation == "submit":
            from apps.domains.bulk_reservations import BulkOperationPaused
            if not isinstance(exc, BulkOperationPaused):
                DomainContactChange.objects.filter(pk=identifier, token=token, status="queued").update(status="failed")
            DomainContactChange.objects.filter(pk=identifier, token=token, status__in=["preparing", "submitting"]).update(status="unknown")
        DomainContactChange.objects.filter(pk=identifier, token=token).update(
            available_at=timezone.now() + timedelta(minutes=5))
    finally:
        DomainContactChange.objects.filter(pk=identifier, token=token).update(lease_until=None)


@shared_task(name="domains.dispatch_contact_changes", ignore_result=True, soft_time_limit=20, time_limit=30)
def dispatch_contact_changes():
    now = timezone.now()
    deadline = monotonic() + 8
    # Never reclaim interrupted preparation or submission as a fresh mutation.
    stale = list(DomainContactChange.objects.exclude(bulk_item__paused_at__isnull=False).filter(status__in=["preparing", "submitting"],
        lease_until__lt=now).values_list("pk", flat=True)[:100])
    for identifier in stale:
        if monotonic() >= deadline:
            return
        DomainContactChange.objects.filter(pk=identifier, status__in=["preparing", "submitting"],
            lease_until__lt=now).update(status="unknown", lease_until=None)
    due = DomainContactChange.objects.exclude(bulk_item__paused_at__isnull=False).filter(available_at__lte=now).filter(
        Q(status__in=["queued", "awaiting"]) | Q(status="unknown") & ~Q(action_id="")
    ).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now)).order_by("available_at", "pk")
    for identifier in due.values_list("pk", flat=True)[:100]:
        if monotonic() >= deadline:
            return
        publish_contact_change(identifier)
