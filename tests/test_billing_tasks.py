"""Tests for Phase 1 billing automation tasks:
  - expire_overdue_quotes (+ beat registration)
  - send_dunning_reminders
  - generate_renewal_invoices
  - auto_suspend_overdue_accounts
  - ensure_billing_schedules beat registration
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_user(django_user_model, email=None, is_staff=False):
    email = email or f"u{uuid.uuid4().hex[:6]}@example.com"
    return django_user_model.objects.create_user(
        email=email, password="pass1234!", is_staff=is_staff
    )


def make_branding():
    from apps.billing.models import BillingDocumentBranding

    b = BillingDocumentBranding.get_solo()
    b.default_vat_rate = Decimal("0.00")
    b.default_due_days = 14
    b.save()
    return b


def make_invoice(user, *, due_days_ago=15, status=None):
    """Create a simple unpaid invoice due *due_days_ago* days in the past."""
    from apps.billing.models import Invoice, InvoiceLineItem
    from apps.billing.numbering import next_invoice_number

    make_branding()
    due = timezone.now().date() - timedelta(days=due_days_ago)
    invoice = Invoice.objects.create(
        user=user,
        number=next_invoice_number(),
        status=status or Invoice.STATUS_UNPAID,
        due_date=due,
        currency="GBP",
        subtotal=Decimal("50.00"),
        vat_rate=Decimal("0.00"),
        vat_amount=Decimal("0.00"),
        total=Decimal("50.00"),
    )
    InvoiceLineItem.objects.create(
        invoice=invoice,
        description="Hosting",
        quantity=Decimal("1"),
        unit_price=Decimal("50.00"),
        line_total=Decimal("50.00"),
    )
    return invoice


def make_package():
    from apps.products.models import Package

    return Package.objects.create(
        name=f"Starter {uuid.uuid4().hex[:4]}",
        slug=f"starter-{uuid.uuid4().hex[:4]}",
        price_monthly=Decimal("9.99"),
        price_annually=Decimal("99.99"),
        is_active=True,
    )


def make_service(user, *, next_due_days=10, billing_period="monthly", cpanel_username=""):
    from apps.services.models import Service

    pkg = make_package()
    return Service.objects.create(
        user=user,
        package=pkg,
        status=Service.STATUS_ACTIVE,
        domain_name="example.com",
        cpanel_username=cpanel_username,
        billing_period=billing_period,
        next_due_date=timezone.now().date() + timedelta(days=next_due_days),
    )


# ---------------------------------------------------------------------------
# expire_overdue_quotes
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_expire_overdue_quotes_flips_sent_and_viewed(django_user_model):
    from apps.billing.models import BillingDocumentBranding, Quote
    from apps.billing.numbering import next_quote_number
    from apps.billing.tasks import expire_overdue_quotes

    make_branding()
    user = make_user(django_user_model)
    past = timezone.now().date() - timedelta(days=3)

    q1 = Quote.objects.create(
        user=user,
        number=next_quote_number(),
        status=Quote.STATUS_SENT,
        valid_until=past,
        currency="GBP",
        total=Decimal("100.00"),
        subtotal=Decimal("100.00"),
        vat_rate=Decimal("0.00"),
        vat_amount=Decimal("0.00"),
    )
    q2 = Quote.objects.create(
        user=user,
        number=next_quote_number(),
        status=Quote.STATUS_VIEWED,
        valid_until=past,
        currency="GBP",
        total=Decimal("100.00"),
        subtotal=Decimal("100.00"),
        vat_rate=Decimal("0.00"),
        vat_amount=Decimal("0.00"),
    )
    # A draft quote (should NOT be expired).
    q3 = Quote.objects.create(
        user=user,
        number=next_quote_number(),
        status=Quote.STATUS_DRAFT,
        valid_until=past,
        currency="GBP",
        total=Decimal("100.00"),
        subtotal=Decimal("100.00"),
        vat_rate=Decimal("0.00"),
        vat_amount=Decimal("0.00"),
    )

    count = expire_overdue_quotes()
    assert count == 2

    q1.refresh_from_db()
    q2.refresh_from_db()
    q3.refresh_from_db()
    assert q1.status == Quote.STATUS_EXPIRED
    assert q2.status == Quote.STATUS_EXPIRED
    assert q3.status == Quote.STATUS_DRAFT  # untouched


@pytest.mark.django_db
def test_expire_overdue_quotes_ignores_future_quotes(django_user_model):
    from apps.billing.models import BillingDocumentBranding, Quote
    from apps.billing.numbering import next_quote_number
    from apps.billing.tasks import expire_overdue_quotes

    make_branding()
    user = make_user(django_user_model)
    future = timezone.now().date() + timedelta(days=5)

    Quote.objects.create(
        user=user,
        number=next_quote_number(),
        status=Quote.STATUS_SENT,
        valid_until=future,
        currency="GBP",
        total=Decimal("100.00"),
        subtotal=Decimal("100.00"),
        vat_rate=Decimal("0.00"),
        vat_amount=Decimal("0.00"),
    )
    count = expire_overdue_quotes()
    assert count == 0


# ---------------------------------------------------------------------------
# send_dunning_reminders
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@patch("apps.notifications.services.send_notification")
def test_dunning_sends_on_day_1(mock_notify, django_user_model):
    from apps.billing.tasks import send_dunning_reminders

    user = make_user(django_user_model)
    invoice = make_invoice(user, due_days_ago=1)

    sent = send_dunning_reminders()
    assert sent == 1
    mock_notify.assert_called_once()
    args, kwargs = mock_notify.call_args
    assert args[0] == "invoice_overdue"
    assert args[1] == user

    invoice.refresh_from_db()
    assert invoice.last_dunning_sent_at is not None


@pytest.mark.django_db
@patch("apps.notifications.services.send_notification")
def test_dunning_sends_on_days_7_14_30(mock_notify, django_user_model):
    from apps.billing.tasks import send_dunning_reminders

    user = make_user(django_user_model)
    for days in (7, 14, 30):
        make_invoice(user, due_days_ago=days)

    sent = send_dunning_reminders()
    assert sent == 3
    assert mock_notify.call_count == 3


@pytest.mark.django_db
@patch("apps.notifications.services.send_notification")
def test_dunning_not_on_non_threshold_days(mock_notify, django_user_model):
    from apps.billing.tasks import send_dunning_reminders

    user = make_user(django_user_model)
    make_invoice(user, due_days_ago=3)   # day 3 — not a dunning day
    make_invoice(user, due_days_ago=10)  # day 10 — not a dunning day

    sent = send_dunning_reminders()
    assert sent == 0
    mock_notify.assert_not_called()


@pytest.mark.django_db
@patch("apps.notifications.services.send_notification")
def test_dunning_skips_recently_notified(mock_notify, django_user_model):
    """If last_dunning_sent_at < 23 hours ago, do not resend."""
    from apps.billing.tasks import send_dunning_reminders

    user = make_user(django_user_model)
    invoice = make_invoice(user, due_days_ago=7)
    # Pretend we already sent a dunning email 2 hours ago.
    invoice.last_dunning_sent_at = timezone.now() - timedelta(hours=2)
    invoice.save(update_fields=["last_dunning_sent_at"])

    sent = send_dunning_reminders()
    assert sent == 0
    mock_notify.assert_not_called()


@pytest.mark.django_db
@patch("apps.notifications.services.send_notification")
def test_dunning_skips_paid_invoices(mock_notify, django_user_model):
    from apps.billing.models import Invoice
    from apps.billing.tasks import send_dunning_reminders

    user = make_user(django_user_model)
    make_invoice(user, due_days_ago=7, status=Invoice.STATUS_PAID)

    sent = send_dunning_reminders()
    assert sent == 0
    mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# generate_renewal_invoices
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@patch("apps.notifications.services.send_notification")
def test_renewal_invoices_created_for_due_services(mock_notify, django_user_model):
    from apps.billing.models import Invoice
    from apps.billing.tasks import generate_renewal_invoices
    from apps.services.models import Service

    make_branding()
    user = make_user(django_user_model)
    service = make_service(user, next_due_days=5)  # due in 5 days (within default 14)
    original_due = service.next_due_date

    created = generate_renewal_invoices(advance_days=14)
    assert created == 1

    service.refresh_from_db()
    # next_due_date should have advanced by one month.
    assert service.next_due_date == original_due + timedelta(days=31) or (
        service.next_due_date.month != original_due.month
        or service.next_due_date.year != original_due.year
    )
    assert service.invoice_id is not None

    invoice = Invoice.objects.get(pk=service.invoice_id)
    assert invoice.status == Invoice.STATUS_UNPAID
    assert invoice.user == user


@pytest.mark.django_db
@patch("apps.notifications.services.send_notification")
def test_renewal_invoices_skips_not_yet_due(mock_notify, django_user_model):
    from apps.billing.tasks import generate_renewal_invoices

    make_branding()
    user = make_user(django_user_model)
    make_service(user, next_due_days=30)  # due in 30 days — outside advance_days=14

    created = generate_renewal_invoices(advance_days=14)
    assert created == 0


@pytest.mark.django_db
@patch("apps.notifications.services.send_notification")
def test_renewal_invoices_skips_inactive_service(mock_notify, django_user_model):
    from apps.billing.tasks import generate_renewal_invoices
    from apps.services.models import Service

    make_branding()
    user = make_user(django_user_model)
    svc = make_service(user, next_due_days=5)
    svc.status = Service.STATUS_SUSPENDED
    svc.save()

    created = generate_renewal_invoices(advance_days=14)
    assert created == 0


@pytest.mark.django_db
@patch("apps.notifications.services.send_notification")
def test_renewal_invoices_deduplication(mock_notify, django_user_model):
    """Running the task twice within 24h should not create a second invoice."""
    from apps.billing.tasks import generate_renewal_invoices

    make_branding()
    user = make_user(django_user_model)
    make_service(user, next_due_days=5)

    first = generate_renewal_invoices(advance_days=14)
    assert first == 1

    second = generate_renewal_invoices(advance_days=14)
    assert second == 0  # skipped because invoice was just created


@pytest.mark.django_db
@patch("apps.notifications.services.send_notification")
def test_renewal_invoice_annually_advances_by_year(mock_notify, django_user_model):
    from apps.billing.tasks import generate_renewal_invoices

    make_branding()
    user = make_user(django_user_model)
    service = make_service(user, next_due_days=5, billing_period="annually")
    original_due = service.next_due_date

    generate_renewal_invoices(advance_days=14)

    service.refresh_from_db()
    # The year part of next_due_date should advance by 1.
    assert service.next_due_date.year == original_due.year + 1


# ---------------------------------------------------------------------------
# auto_suspend_overdue_accounts
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@patch("apps.provisioning.whm_client.WHMClient")
@patch("apps.notifications.services.send_notification")
def test_suspension_suspends_overdue_service(mock_notify, mock_whm_cls, django_user_model):
    from apps.billing.tasks import auto_suspend_overdue_accounts
    from apps.services.models import Service

    mock_whm = MagicMock()
    mock_whm_cls.return_value = mock_whm

    make_branding()
    user = make_user(django_user_model)
    # Invoice overdue by 35 days (past the 30-day default threshold).
    make_invoice(user, due_days_ago=35, status="overdue")
    service = make_service(user, cpanel_username="testcpanel")

    count = auto_suspend_overdue_accounts(suspend_after_days=30)
    assert count == 1

    mock_whm.suspend_account.assert_called_once_with(
        "testcpanel",
        reason="Overdue invoice — suspended after 30 days",
    )
    service.refresh_from_db()
    assert service.status == Service.STATUS_SUSPENDED
    mock_notify.assert_called_once()


@pytest.mark.django_db
@patch("apps.provisioning.whm_client.WHMClient")
@patch("apps.notifications.services.send_notification")
def test_suspension_ignores_service_without_cpanel(mock_notify, mock_whm_cls, django_user_model):
    from apps.billing.tasks import auto_suspend_overdue_accounts

    make_branding()
    user = make_user(django_user_model)
    make_invoice(user, due_days_ago=35, status="overdue")
    make_service(user, cpanel_username="")  # no cPanel username

    count = auto_suspend_overdue_accounts(suspend_after_days=30)
    assert count == 0


@pytest.mark.django_db
@patch("apps.provisioning.whm_client.WHMClient")
@patch("apps.notifications.services.send_notification")
def test_suspension_ignores_within_threshold(mock_notify, mock_whm_cls, django_user_model):
    from apps.billing.tasks import auto_suspend_overdue_accounts

    make_branding()
    user = make_user(django_user_model)
    # Invoice only 10 days overdue — below 30-day threshold.
    make_invoice(user, due_days_ago=10)
    make_service(user, cpanel_username="testcpanel")

    count = auto_suspend_overdue_accounts(suspend_after_days=30)
    assert count == 0


@pytest.mark.django_db
@patch("apps.provisioning.whm_client.WHMClient")
@patch("apps.notifications.services.send_notification")
def test_suspension_ignores_already_suspended(mock_notify, mock_whm_cls, django_user_model):
    from apps.billing.tasks import auto_suspend_overdue_accounts
    from apps.services.models import Service

    make_branding()
    user = make_user(django_user_model)
    make_invoice(user, due_days_ago=35, status="overdue")
    svc = make_service(user, cpanel_username="testcpanel")
    svc.status = Service.STATUS_SUSPENDED
    svc.save()

    count = auto_suspend_overdue_accounts(suspend_after_days=30)
    assert count == 0


@pytest.mark.django_db
@patch("apps.provisioning.whm_client.WHMClient")
@patch("apps.notifications.services.send_notification")
def test_suspension_whm_error_is_logged_not_raised(mock_notify, mock_whm_cls, django_user_model):
    from apps.billing.tasks import auto_suspend_overdue_accounts
    from apps.provisioning.whm_client import WHMClientError

    mock_whm = MagicMock()
    mock_whm.suspend_account.side_effect = WHMClientError("connection refused")
    mock_whm_cls.return_value = mock_whm

    make_branding()
    user = make_user(django_user_model)
    make_invoice(user, due_days_ago=35, status="overdue")
    make_service(user, cpanel_username="testcpanel")

    # Should not raise; should return 0 (not suspended due to error).
    count = auto_suspend_overdue_accounts(suspend_after_days=30)
    assert count == 0


# ---------------------------------------------------------------------------
# Beat registration
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ensure_billing_schedules_registers_all_tasks():
    from apps.billing.tasks import (
        DUNNING_TASK_NAME,
        EXPIRE_QUOTES_TASK_NAME,
        RENEWAL_INVOICES_TASK_NAME,
        SUSPENSION_TASK_NAME,
        ensure_billing_schedules,
    )
    from django_celery_beat.models import PeriodicTask

    ensure_billing_schedules()

    names = set(PeriodicTask.objects.values_list("name", flat=True))
    for expected in (
        EXPIRE_QUOTES_TASK_NAME,
        DUNNING_TASK_NAME,
        RENEWAL_INVOICES_TASK_NAME,
        SUSPENSION_TASK_NAME,
    ):
        assert expected in names, f"PeriodicTask '{expected}' not registered"


@pytest.mark.django_db
def test_ensure_billing_schedules_is_idempotent():
    from apps.billing.tasks import ensure_billing_schedules
    from django_celery_beat.models import PeriodicTask

    ensure_billing_schedules()
    before = PeriodicTask.objects.count()
    ensure_billing_schedules()
    after = PeriodicTask.objects.count()
    assert before == after
