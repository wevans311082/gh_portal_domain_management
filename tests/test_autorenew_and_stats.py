"""
Tests for:
  - process_auto_renewals Celery task
  - Admin stats view (/admin-tools/stats/)
  - core_tags templatetag filters
"""
import pytest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.urls import reverse
from django.utils import timezone


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def make_user(django_user_model, email="auto@example.com", is_staff=False):
    return django_user_model.objects.create_user(
        email=email, password="testpass123", is_staff=is_staff
    )


def make_domain(user, name="autorenew.com", expires_days=5, auto_renew=True, registrar_id="RC123"):
    from apps.domains.models import Domain
    return Domain.objects.create(
        user=user,
        name=name,
        tld="com",
        status=Domain.STATUS_ACTIVE,
        auto_renew=auto_renew,
        registrar_id=registrar_id,
        expires_at=timezone.now().date() + timedelta(days=expires_days),
    )


def make_tld_pricing(tld="com", renewal_cost="10.00"):
    from apps.domains.models import TLDPricing
    obj, _ = TLDPricing.objects.get_or_create(
        tld=tld,
        defaults={
            "renewal_cost": Decimal(renewal_cost),
            "registration_cost": Decimal("10.00"),
            "is_active": True,
        },
    )
    return obj


# ─────────────────────────────────────────────
# process_auto_renewals task
# ─────────────────────────────────────────────

@pytest.mark.django_db
@patch("apps.domains.tasks.execute_domain_renewal")
def test_auto_renew_creates_renewal_for_expiring_domain(mock_exec, django_user_model):
    from apps.domains.tasks import process_auto_renewals
    from apps.domains.models import DomainRenewal

    user = make_user(django_user_model)
    domain = make_domain(user)
    make_tld_pricing()

    result = process_auto_renewals(days_ahead=7)

    assert result == 1
    renewal = DomainRenewal.objects.get(domain=domain)
    assert renewal.status == DomainRenewal.STATUS_PAID
    assert renewal.renewal_years == 1
    mock_exec.delay.assert_called_once_with(renewal.id)


@pytest.mark.django_db
@patch("apps.domains.tasks.execute_domain_renewal")
def test_auto_renew_skips_domain_without_auto_renew(mock_exec, django_user_model):
    from apps.domains.tasks import process_auto_renewals

    user = make_user(django_user_model, email="no_ar@example.com")
    make_domain(user, name="norenew.com", auto_renew=False)
    make_tld_pricing()

    result = process_auto_renewals(days_ahead=7)
    assert result == 0
    mock_exec.delay.assert_not_called()


@pytest.mark.django_db
@patch("apps.domains.tasks.execute_domain_renewal")
def test_auto_renew_skips_domain_not_expiring_yet(mock_exec, django_user_model):
    from apps.domains.tasks import process_auto_renewals

    user = make_user(django_user_model, email="far@example.com")
    make_domain(user, name="farout.com", expires_days=60)
    make_tld_pricing()

    result = process_auto_renewals(days_ahead=7)
    assert result == 0
    mock_exec.delay.assert_not_called()


@pytest.mark.django_db
@patch("apps.domains.tasks.execute_domain_renewal")
def test_auto_renew_skips_if_renewal_already_exists(mock_exec, django_user_model):
    from apps.domains.tasks import process_auto_renewals
    from apps.domains.models import DomainRenewal
    from apps.billing.models import Invoice

    user = make_user(django_user_model, email="exists@example.com")
    domain = make_domain(user, name="already.com")
    make_tld_pricing()

    invoice = Invoice.objects.create(
        user=user,
        number="INV-AR-EXISTING",
        status=Invoice.STATUS_PAID,
        vat_rate=Decimal("0.00"),
        due_date=timezone.now().date(),
        paid_at=timezone.now(),
    )
    DomainRenewal.objects.create(
        domain=domain,
        user=user,
        invoice=invoice,
        renewal_years=1,
        total_price=Decimal("12.50"),
        status=DomainRenewal.STATUS_PROCESSING,
    )

    result = process_auto_renewals(days_ahead=7)
    assert result == 0
    mock_exec.delay.assert_not_called()


@pytest.mark.django_db
@patch("apps.domains.tasks.execute_domain_renewal")
def test_auto_renew_skips_no_tld_pricing(mock_exec, django_user_model):
    from apps.domains.tasks import process_auto_renewals

    user = make_user(django_user_model, email="notld@example.com")
    make_domain(user, name="notld.xyz")
    # Deliberately no TLDPricing for .xyz

    result = process_auto_renewals(days_ahead=7)
    assert result == 0
    mock_exec.delay.assert_not_called()


@pytest.mark.django_db
@patch("apps.domains.tasks.execute_domain_renewal")
def test_auto_renew_failed_renewal_gets_re_queued(mock_exec, django_user_model):
    """A domain with a previously FAILED renewal should be re-queued."""
    from apps.domains.tasks import process_auto_renewals
    from apps.domains.models import DomainRenewal
    from apps.billing.models import Invoice

    user = make_user(django_user_model, email="retry@example.com")
    domain = make_domain(user, name="retry.com")
    make_tld_pricing()

    invoice = Invoice.objects.create(
        user=user,
        number="INV-AR-FAILED",
        status=Invoice.STATUS_PAID,
        vat_rate=Decimal("0.00"),
        due_date=timezone.now().date(),
        paid_at=timezone.now(),
    )
    DomainRenewal.objects.create(
        domain=domain,
        user=user,
        invoice=invoice,
        renewal_years=1,
        total_price=Decimal("12.50"),
        status=DomainRenewal.STATUS_FAILED,
    )

    result = process_auto_renewals(days_ahead=7)
    # A new renewal should have been created (not the failed one)
    assert result == 1
    mock_exec.delay.assert_called_once()


@pytest.mark.django_db
@patch("apps.domains.tasks.execute_domain_renewal")
def test_auto_renew_creates_paid_invoice(mock_exec, django_user_model):
    from apps.domains.tasks import process_auto_renewals
    from apps.domains.models import DomainRenewal
    from apps.billing.models import Invoice

    user = make_user(django_user_model, email="invoice@example.com")
    make_domain(user, name="invoicedomain.com")
    make_tld_pricing()

    process_auto_renewals(days_ahead=7)
    renewal = DomainRenewal.objects.get(domain__name="invoicedomain.com")
    invoice = Invoice.objects.get(id=renewal.invoice_id)
    assert invoice.status == Invoice.STATUS_PAID
    assert invoice.line_items.count() == 1


# ─────────────────────────────────────────────
# Admin stats view
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_stats_requires_staff(client, django_user_model):
    user = make_user(django_user_model, email="plain@example.com", is_staff=False)
    client.force_login(user)
    response = client.get(reverse("admin_tools:stats"))
    # staff_member_required redirects non-staff to login
    assert response.status_code in (302, 403)


@pytest.mark.django_db
def test_stats_renders_for_staff(client, django_user_model):
    staff = make_user(django_user_model, email="staff@example.com", is_staff=True)
    client.force_login(staff)
    response = client.get(reverse("admin_tools:stats"))
    assert response.status_code == 200
    assert b"Stats" in response.content


@pytest.mark.django_db
def test_stats_shows_expiring_domains(client, django_user_model):
    staff = make_user(django_user_model, email="staff2@example.com", is_staff=True)
    owner = make_user(django_user_model, email="owner@example.com")
    make_domain(owner, name="expiring-soon.com", expires_days=10)
    client.force_login(staff)
    response = client.get(reverse("admin_tools:stats"))
    assert b"expiring-soon.com" in response.content


@pytest.mark.django_db
def test_stats_does_not_show_far_expiry(client, django_user_model):
    staff = make_user(django_user_model, email="staff3@example.com", is_staff=True)
    owner = make_user(django_user_model, email="owner2@example.com")
    make_domain(owner, name="farfuture.com", expires_days=60)
    client.force_login(staff)
    response = client.get(reverse("admin_tools:stats"))
    assert b"farfuture.com" not in response.content


# ─────────────────────────────────────────────
# core_tags templatetag unit tests
# ─────────────────────────────────────────────

def test_zip_lists_filter():
    from apps.core.templatetags.core_tags import zip_lists
    result = list(zip_lists([1, 2, 3], ["a", "b", "c"]))
    assert result == [(1, "a"), (2, "b"), (3, "c")]


def test_zip_lists_mismatched_lengths():
    from apps.core.templatetags.core_tags import zip_lists
    result = list(zip_lists([1, 2], ["a", "b", "c"]))
    assert result == [(1, "a"), (2, "b")]


def test_list_max_filter():
    from apps.core.templatetags.core_tags import list_max
    assert list_max([3, 1, 4, 1, 5]) == 5
    assert list_max([]) == 0


def test_pct_of_filter():
    from apps.core.templatetags.core_tags import pct_of
    assert pct_of(50, 200) == 25
    assert pct_of(0, 100) == 0
    assert pct_of(100, 0) == 0


def test_currency_filter():
    from apps.core.templatetags.core_tags import currency
    assert currency(9.99) == "£9.99"
    assert currency("bad") == "bad"


# ─────────────────────────────────────────────
# ensure_auto_renew_schedule Beat registration
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_ensure_auto_renew_schedule_creates_periodic_task():
    from apps.domains.tasks import ensure_auto_renew_schedule, AUTO_RENEW_TASK_NAME
    from django_celery_beat.models import PeriodicTask

    task = ensure_auto_renew_schedule()
    assert task.name == AUTO_RENEW_TASK_NAME
    assert task.enabled is True
    assert task.interval.every == 24


@pytest.mark.django_db
def test_ensure_auto_renew_schedule_is_idempotent():
    from apps.domains.tasks import ensure_auto_renew_schedule, AUTO_RENEW_TASK_NAME
    from django_celery_beat.models import PeriodicTask

    ensure_auto_renew_schedule()
    ensure_auto_renew_schedule()  # second call must not raise or duplicate
    assert PeriodicTask.objects.filter(name=AUTO_RENEW_TASK_NAME).count() == 1
