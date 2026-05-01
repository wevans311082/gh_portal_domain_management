import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Invoice, InvoiceLineItem
from apps.domains.models import Domain, DomainContact, DomainRenewal, TLDPricing
from apps.domains.tasks import execute_domain_renewal


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def make_user(django_user_model, email="renew@example.com"):
    return django_user_model.objects.create_user(email=email, password="testpass123")


def make_domain(user, name="example.com", tld="com", status=Domain.STATUS_ACTIVE, registrar_id="RC-1234"):
    return Domain.objects.create(
        user=user,
        name=name,
        tld=tld,
        status=status,
        registrar_id=registrar_id,
        expires_at=datetime.date(2026, 6, 1),
    )


def make_pricing(tld="com", renewal_cost=Decimal("8.00")):
    return TLDPricing.objects.create(
        tld=tld,
        renewal_cost=renewal_cost,
        registration_cost=Decimal("10.00"),
        profit_margin_percentage=Decimal("25.00"),
        is_active=True,
    )


def make_invoice(user, total=Decimal("10.00")):
    invoice = Invoice.objects.create(
        user=user,
        number=f"RNW-TEST-{timezone.now().timestamp()}",
        status=Invoice.STATUS_UNPAID,
        vat_rate=Decimal("0.00"),
        due_date=timezone.now().date(),
    )
    invoice.calculate_totals()
    return invoice


def make_renewal(domain, user, invoice, years=1, price=Decimal("10.00"), status=DomainRenewal.STATUS_PENDING_PAYMENT):
    return DomainRenewal.objects.create(
        domain=domain,
        user=user,
        invoice=invoice,
        renewal_years=years,
        total_price=price,
        status=status,
    )


# ──────────────────────────────────────────────
# domain_renew view — GET
# ──────────────────────────────────────────────

@pytest.mark.django_db
def test_renew_view_get_shows_price(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user)
    make_pricing()
    client.force_login(user)
    url = reverse("domains:renew", kwargs={"pk": domain.pk})
    response = client.get(url)
    assert response.status_code == 200
    assert b"Renew Domain" in response.content
    assert b"example.com" in response.content


@pytest.mark.django_db
def test_renew_view_requires_login(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user)
    url = reverse("domains:renew", kwargs={"pk": domain.pk})
    response = client.get(url)
    assert response.status_code == 302
    assert "login" in response["Location"] or "accounts" in response["Location"]


@pytest.mark.django_db
def test_renew_view_rejects_other_user(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user)
    other = django_user_model.objects.create_user(email="other@example.com", password="pass")
    client.force_login(other)
    url = reverse("domains:renew", kwargs={"pk": domain.pk})
    response = client.get(url)
    assert response.status_code == 404


@pytest.mark.django_db
def test_renew_view_rejects_non_renewable_status(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user, status=Domain.STATUS_CANCELLED)
    make_pricing()
    client.force_login(user)
    url = reverse("domains:renew", kwargs={"pk": domain.pk})
    response = client.post(url, {"years": 1})
    assert response.status_code == 302
    # Redirected back to detail, not to payment
    assert "renew" not in response["Location"]


@pytest.mark.django_db
def test_renew_view_no_pricing_redirects(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user)
    # No TLDPricing object
    client.force_login(user)
    url = reverse("domains:renew", kwargs={"pk": domain.pk})
    response = client.post(url, {"years": 1})
    assert response.status_code == 302


# ──────────────────────────────────────────────
# domain_renew view — POST (creates invoice + renewal)
# ──────────────────────────────────────────────

@pytest.mark.django_db
def test_renew_post_creates_invoice_and_renewal(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user)
    make_pricing(renewal_cost=Decimal("8.00"))
    client.force_login(user)
    url = reverse("domains:renew", kwargs={"pk": domain.pk})
    response = client.post(url, {"years": 2})
    # Should redirect to Stripe checkout
    assert response.status_code == 302
    renewal = DomainRenewal.objects.filter(domain=domain, user=user).first()
    assert renewal is not None
    assert renewal.renewal_years == 2
    assert renewal.status == DomainRenewal.STATUS_PENDING_PAYMENT
    invoice = renewal.invoice
    assert invoice.status == Invoice.STATUS_UNPAID
    # renewal_price for 2 years: 8.00 * 1.25 * 2 = 20.00
    assert renewal.total_price == Decimal("20.00")


@pytest.mark.django_db
def test_renew_post_one_year(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user)
    make_pricing(renewal_cost=Decimal("8.00"))
    client.force_login(user)
    url = reverse("domains:renew", kwargs={"pk": domain.pk})
    client.post(url, {"years": 1})
    renewal = DomainRenewal.objects.get(domain=domain)
    assert renewal.renewal_years == 1
    assert renewal.total_price == Decimal("10.00")  # 8 * 1.25


# ──────────────────────────────────────────────
# execute_domain_renewal task
# ──────────────────────────────────────────────

@pytest.mark.django_db
def test_execute_renewal_missing_renewal():
    # Should not raise — task exits early after logging
    execute_domain_renewal(99999)


@pytest.mark.django_db
def test_execute_renewal_already_completed(django_user_model):
    user = make_user(django_user_model, "comp@example.com")
    domain = make_domain(user)
    invoice = make_invoice(user)
    renewal = make_renewal(domain, user, invoice, status=DomainRenewal.STATUS_COMPLETED)
    execute_domain_renewal(renewal.id)
    # Status must remain COMPLETED — task exits without doing anything
    renewal.refresh_from_db()
    assert renewal.status == DomainRenewal.STATUS_COMPLETED


@pytest.mark.django_db
def test_execute_renewal_no_registrar_id(django_user_model):
    user = make_user(django_user_model, "noreg@example.com")
    domain = make_domain(user, registrar_id="")
    invoice = make_invoice(user)
    renewal = make_renewal(domain, user, invoice, status=DomainRenewal.STATUS_PAID)
    execute_domain_renewal(renewal.id)
    renewal.refresh_from_db()
    assert renewal.status == DomainRenewal.STATUS_FAILED
    assert "no registrar order ID" in renewal.last_error


@pytest.mark.django_db
def test_execute_renewal_success(django_user_model, monkeypatch):
    user = make_user(django_user_model, "success@example.com")
    domain = make_domain(user, registrar_id="RC-111")
    invoice = make_invoice(user)
    renewal = make_renewal(domain, user, invoice, years=1, status=DomainRenewal.STATUS_PAID)

    def fake_renew(self, order_id, years, current_expiry_date, auto_renew=True):
        return {"status": "Success", "actionstatus": "Success"}

    from apps.domains import resellerclub_client as rc
    monkeypatch.setattr(rc.ResellerClubClient, "renew_domain", fake_renew)

    execute_domain_renewal(renewal.id)

    renewal.refresh_from_db()
    assert renewal.status == DomainRenewal.STATUS_COMPLETED
    assert renewal.new_expiry_date is not None
    assert renewal.completed_at is not None

    domain.refresh_from_db()
    assert domain.status == Domain.STATUS_ACTIVE
    # New expiry should be 1 year after original (2026-06-01 -> 2027-06-01)
    assert domain.expires_at == datetime.date(2027, 6, 1)


@pytest.mark.django_db
def test_execute_renewal_fails_on_registrar_error(django_user_model, monkeypatch):
    user = make_user(django_user_model, "fail@example.com")
    domain = make_domain(user, registrar_id="RC-222")
    invoice = make_invoice(user)
    renewal = make_renewal(domain, user, invoice, status=DomainRenewal.STATUS_PAID)

    from apps.domains import resellerclub_client as rc
    from apps.domains.resellerclub_client import ResellerClubError

    def fake_renew(self, *args, **kwargs):
        raise ResellerClubError("Registrar rejected renewal")

    monkeypatch.setattr(rc.ResellerClubClient, "renew_domain", fake_renew)

    execute_domain_renewal(renewal.id)

    renewal.refresh_from_db()
    assert renewal.status == DomainRenewal.STATUS_FAILED
    assert "Registrar rejected renewal" in renewal.last_error


# ──────────────────────────────────────────────
# Stripe webhook queues renewal
# ──────────────────────────────────────────────

@pytest.mark.django_db
def test_webhook_queues_renewal_on_payment(django_user_model, monkeypatch):
    from apps.payments.views import _queue_paid_domain_orders

    user = make_user(django_user_model, "webhook@example.com")
    domain = make_domain(user)
    invoice = make_invoice(user)
    renewal = make_renewal(domain, user, invoice, status=DomainRenewal.STATUS_PENDING_PAYMENT)

    queued = []
    monkeypatch.setattr("apps.payments.views.execute_domain_renewal.delay", lambda rid: queued.append(rid))

    _queue_paid_domain_orders(invoice)

    renewal.refresh_from_db()
    assert renewal.status == DomainRenewal.STATUS_PAID
    assert renewal.id in queued
