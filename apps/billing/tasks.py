"""Celery tasks for billing housekeeping."""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

# ── Beat task metadata ───────────────────────────────────────────────────────
EXPIRE_QUOTES_TASK_NAME = "Expire overdue quotes"
EXPIRE_QUOTES_TASK_PATH = "apps.billing.tasks.expire_overdue_quotes"

DUNNING_TASK_NAME = "Send dunning reminders"
DUNNING_TASK_PATH = "apps.billing.tasks.send_dunning_reminders"

RENEWAL_INVOICES_TASK_NAME = "Generate service renewal invoices"
RENEWAL_INVOICES_TASK_PATH = "apps.billing.tasks.generate_renewal_invoices"

SUSPENSION_TASK_NAME = "Auto-suspend overdue accounts"
SUSPENSION_TASK_PATH = "apps.billing.tasks.auto_suspend_overdue_accounts"


def _ensure_daily_task(name: str, task_path: str):
    """Register a 24-hour interval PeriodicTask (idempotent)."""
    from django_celery_beat.models import IntervalSchedule, PeriodicTask

    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=24,
        period=IntervalSchedule.HOURS,
    )
    task, created = PeriodicTask.objects.update_or_create(
        name=name,
        defaults={"task": task_path, "interval": schedule, "enabled": True},
    )
    logger.info("%s beat task: %s", "Registered" if created else "Updated", name)
    return task


def ensure_billing_schedules():
    """Register all billing periodic tasks (called from BillingConfig.ready)."""
    _ensure_daily_task(EXPIRE_QUOTES_TASK_NAME, EXPIRE_QUOTES_TASK_PATH)
    _ensure_daily_task(DUNNING_TASK_NAME, DUNNING_TASK_PATH)
    _ensure_daily_task(RENEWAL_INVOICES_TASK_NAME, RENEWAL_INVOICES_TASK_PATH)
    _ensure_daily_task(SUSPENSION_TASK_NAME, SUSPENSION_TASK_PATH)


# ── Tasks ────────────────────────────────────────────────────────────────────


@shared_task(name="billing.expire_overdue_quotes")
def expire_overdue_quotes() -> int:
    """Flip ``sent``/``viewed`` quotes past their valid_until date to ``expired``.

    Returns the number of quotes affected. Scheduled daily via
    ``ensure_billing_schedules()``.
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


@shared_task(name="billing.send_dunning_reminders")
def send_dunning_reminders() -> int:
    """Send overdue reminder emails for unpaid/overdue invoices.

    Reminders are sent on days 1, 7, 14, and 30 past the due date.
    A reminder is never sent twice within 24 hours regardless of schedule
    drift (``last_dunning_sent_at`` guard).

    Returns the number of reminders sent.
    """
    from apps.billing.models import Invoice
    from apps.notifications.services import send_notification

    DUNNING_DAYS = [1, 7, 14, 30]  # overdue by N days → send reminder
    now = timezone.now()
    today = now.date()
    sent = 0

    overdue_invoices = Invoice.objects.filter(
        status__in=[Invoice.STATUS_UNPAID, Invoice.STATUS_OVERDUE, Invoice.STATUS_PARTIALLY_PAID],
        due_date__lt=today,
    ).select_related("user")

    for invoice in overdue_invoices:
        days_overdue = (today - invoice.due_date).days
        if days_overdue not in DUNNING_DAYS:
            continue

        # Don't re-send if we already sent a reminder in the last 23 hours.
        if invoice.last_dunning_sent_at and (now - invoice.last_dunning_sent_at).total_seconds() < 82800:
            continue

        try:
            send_notification(
                "invoice_overdue",
                invoice.user,
                {
                    "invoice": invoice,
                    "invoice_number": invoice.number,
                    "days_overdue": days_overdue,
                    "amount_outstanding": invoice.amount_outstanding,
                },
            )
            Invoice.objects.filter(pk=invoice.pk).update(last_dunning_sent_at=now)
            sent += 1
            logger.info(
                "Dunning reminder sent for invoice %s (days_overdue=%s, user=%s)",
                invoice.number,
                days_overdue,
                invoice.user_id,
            )
        except Exception as exc:  # pragma: no cover — email is best-effort
            logger.exception("Failed to send dunning email for invoice %s: %s", invoice.number, exc)

    return sent


@shared_task(name="billing.generate_renewal_invoices")
def generate_renewal_invoices(advance_days: int = 14) -> int:
    """Create unpaid renewal invoices for active services due within *advance_days*.

    Skips any service that already has an unpaid or draft invoice generated
    in the last 24 hours to prevent duplicates if the task is triggered more
    than once.  Advances ``Service.next_due_date`` by one billing period on
    invoice creation.

    Returns the number of invoices created.
    """
    from dateutil.relativedelta import relativedelta

    from apps.billing.models import Invoice
    from apps.billing.services import LineItemSpec, create_invoice
    from apps.services.models import Service

    now = timezone.now()
    cutoff = now.date() + timedelta(days=advance_days)
    created_count = 0

    services = Service.objects.filter(
        status=Service.STATUS_ACTIVE,
        next_due_date__isnull=False,
        next_due_date__lte=cutoff,
    ).select_related("user", "package")

    for service in services:
        # Guard: skip if a recent renewal invoice already exists.
        recent_exists = Invoice.objects.filter(
            user=service.user,
            services=service,
            status__in=[Invoice.STATUS_DRAFT, Invoice.STATUS_UNPAID],
            created_at__gte=now - timedelta(hours=24),
        ).exists()
        if recent_exists:
            logger.info(
                "Skipping renewal invoice for service %s — recent draft/unpaid invoice exists",
                service.pk,
            )
            continue

        unit_price = (
            service.package.price_annually
            if service.billing_period == "annually"
            else service.package.price_monthly
        )
        description = (
            f"{service.package.name} hosting renewal"
            f" ({service.domain_name or 'no domain'})"
            f" — {service.billing_period}"
        )

        try:
            invoice = create_invoice(
                user=service.user,
                line_items=[LineItemSpec(description=description, unit_price=unit_price)],
                source_kind=Invoice.SOURCE_SERVICE_ORDER,
                send_email=True,
            )

            # Link the service to the new invoice.
            service.invoice = invoice
            # Advance next_due_date by one billing period.
            if service.billing_period == "annually":
                service.next_due_date = service.next_due_date + relativedelta(years=1)
            else:
                service.next_due_date = service.next_due_date + relativedelta(months=1)
            service.save(update_fields=["invoice", "next_due_date", "updated_at"])

            created_count += 1
            logger.info(
                "Renewal invoice %s created for service %s (user=%s)",
                invoice.number,
                service.pk,
                service.user_id,
            )
        except Exception as exc:  # pragma: no cover — don't let one failure abort the whole run
            logger.exception("Failed to create renewal invoice for service %s: %s", service.pk, exc)

    return created_count


@shared_task(name="billing.auto_suspend_overdue_accounts")
def auto_suspend_overdue_accounts(suspend_after_days: int | None = None) -> int:
    """Suspend cPanel hosting accounts whose invoices are overdue by more than
    *suspend_after_days* (default: runtime setting ``DUNNING_SUSPEND_DAYS``,
    fallback 30).

    Only suspends services that have a ``cpanel_username`` set and are still
    ``active``.  Sets ``Service.status = suspended`` and sends the
    ``hosting_suspended`` notification email.

    Returns the number of services suspended.
    """
    from apps.billing.models import Invoice
    from apps.core.runtime_settings import get_runtime_int
    from apps.notifications.services import send_notification
    from apps.provisioning.whm_client import WHMClient, WHMClientError
    from apps.services.models import Service

    if suspend_after_days is None:
        suspend_after_days = get_runtime_int("DUNNING_SUSPEND_DAYS", 30)

    cutoff_date = timezone.now().date() - timedelta(days=suspend_after_days)

    # Find active services linked to overdue invoices older than the threshold.
    overdue_invoice_user_ids = (
        Invoice.objects.filter(
            status__in=[Invoice.STATUS_UNPAID, Invoice.STATUS_OVERDUE],
            due_date__lt=cutoff_date,
        )
        .values_list("user_id", flat=True)
        .distinct()
    )

    services_to_suspend = Service.objects.filter(
        status=Service.STATUS_ACTIVE,
        cpanel_username__gt="",  # only services with a cPanel account
        user_id__in=overdue_invoice_user_ids,
    ).select_related("user")

    suspended_count = 0
    whm = WHMClient()

    for service in services_to_suspend:
        try:
            whm.suspend_account(
                service.cpanel_username,
                reason=f"Overdue invoice — suspended after {suspend_after_days} days",
            )
            service.status = Service.STATUS_SUSPENDED
            service.save(update_fields=["status", "updated_at"])

            try:
                send_notification(
                    "hosting_suspended",
                    service.user,
                    {"service": service, "domain": service.domain_name},
                )
            except Exception as email_exc:  # pragma: no cover
                logger.exception(
                    "Failed to send suspension email for service %s: %s", service.pk, email_exc
                )

            suspended_count += 1
            logger.info(
                "Service %s (cpanel=%s, user=%s) suspended for overdue invoices.",
                service.pk,
                service.cpanel_username,
                service.user_id,
            )
        except WHMClientError as exc:
            logger.error(
                "WHM suspend failed for service %s (cpanel=%s): %s",
                service.pk,
                service.cpanel_username,
                exc,
            )

    return suspended_count
