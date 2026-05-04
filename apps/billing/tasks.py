"""Celery tasks for billing housekeeping."""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="billing.expire_overdue_quotes")
def expire_overdue_quotes() -> int:
    """Flip ``sent``/``viewed`` quotes past their valid_until date to ``expired``.

    Returns the number of quotes affected. Schedule via django-celery-beat
    to run daily.
    """
    from apps.billing.models import Quote

    today = timezone.now().date()
    qs = Quote.objects.filter(
        status__in=[Quote.STATUS_SENT, Quote.STATUS_VIEWED],
        valid_until__lt=today,
    )
    count = qs.update(status=Quote.STATUS_EXPIRED)
    if count:
        logger.info("Expired %s quote(s)", count)
    return count
