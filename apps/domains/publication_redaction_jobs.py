"""Bounded redaction delivery with durable mutation fences and safe read retries."""

from datetime import timedelta
from time import monotonic

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.domains.models import PublicationRedaction


def publish_redaction(identifier):
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return False
    try:
        run_publication_redaction.delay(identifier)
    except Exception:
        return False
    return True


@shared_task(name="domains.run_publication_redaction", ignore_result=True, soft_time_limit=100, time_limit=120)
def run_publication_redaction(identifier):
    from apps.core.abuse import consume_limit, AbuseLimitExceeded, AbuseStoreUnavailable
    from apps.domains.publication_redaction import process_publication_redaction
    from apps.domains.resellerclub_client import ResellerClubClient

    with transaction.atomic():
        job = PublicationRedaction.objects.select_for_update().get(pk=identifier)
        now = timezone.now()
        if (job.status not in {"queued", "review", "awaiting", "unknown"} or job.available_at > now
                or job.lease_until is not None and job.lease_until > now):
            return
        try:
            consume_limit("registrar-contact-change-worker", "global", 20, 60)
        except (AbuseLimitExceeded, AbuseStoreUnavailable) as exc:
            job.available_at = now + timedelta(seconds=getattr(exc, "retry_after", 60))
            job.save(update_fields=["available_at", "updated_at"])
            return
        job.lease_until = now + timedelta(minutes=3)
        job.save(update_fields=["lease_until", "updated_at"])
        token = job.token
    try:
        process_publication_redaction(identifier, client=ResellerClubClient())
    except Exception as exc:
        if isinstance(exc, ValidationError):
            PublicationRedaction.objects.filter(pk=identifier, token=token, status__in=["queued", "review"]).update(status="review")
        PublicationRedaction.objects.filter(pk=identifier, token=token, status="submitting").update(status="unknown")
        PublicationRedaction.objects.filter(pk=identifier, token=token).exclude(status="confirmed").update(
            available_at=timezone.now() + timedelta(minutes=5))
    finally:
        PublicationRedaction.objects.filter(pk=identifier, token=token).update(lease_until=None)


@shared_task(name="domains.dispatch_publication_redactions", ignore_result=True, soft_time_limit=20, time_limit=30)
def dispatch_publication_redactions():
    now = timezone.now()
    deadline = monotonic() + 8
    for identifier in PublicationRedaction.objects.filter(status="submitting", lease_until__lt=now).values_list("pk", flat=True)[:100]:
        if monotonic() >= deadline:
            return
        PublicationRedaction.objects.filter(pk=identifier, status="submitting", lease_until__lt=now).update(status="unknown", lease_until=None)
    due = PublicationRedaction.objects.filter(status__in=["queued", "review", "awaiting", "unknown"], available_at__lte=now).filter(
        Q(lease_until__isnull=True) | Q(lease_until__lte=now)).order_by("available_at", "pk")
    for identifier in due.values_list("pk", flat=True)[:100]:
        if monotonic() >= deadline:
            return
        publish_redaction(identifier)
