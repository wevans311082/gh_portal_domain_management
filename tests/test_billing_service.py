"""Tests for the canonical billing service + public quote builder."""
from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone


def make_user(django_user_model, email=None):
    email = email or f"u{uuid.uuid4().hex[:6]}@example.com"
    return django_user_model.objects.create_user(email=email, password="pass1234!")


# ---------------------------------------------------------------------------
# Branding singleton
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_branding_get_solo_creates_with_defaults():
    from apps.billing.models import BillingDocumentBranding

    branding = BillingDocumentBranding.get_solo()
    assert branding.pk is not None
    again = BillingDocumentBranding.get_solo()
    assert again.pk == branding.pk
    assert branding.default_currency
    assert branding.invoice_number_format
    assert branding.quote_number_format


@pytest.mark.django_db
def test_invoice_numbering_is_unique_and_uses_format():
    from apps.billing.models import BillingDocumentBranding
    from apps.billing.numbering import next_invoice_number

    branding = BillingDocumentBranding.get_solo()
    branding.invoice_number_format = "INV-{seq:04d}"
    branding.invoice_seq = 0
    branding.save()

    nums = {next_invoice_number() for _ in range(5)}
    assert len(nums) == 5  # all unique
    assert all(n.startswith("INV-") for n in nums)


# ---------------------------------------------------------------------------
# create_invoice
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_invoice_applies_branding_defaults(django_user_model):
    from apps.billing.models import BillingDocumentBranding, Invoice
    from apps.billing.services import LineItemSpec, create_invoice

    user = make_user(django_user_model)
    branding = BillingDocumentBranding.get_solo()
    branding.default_vat_rate = Decimal("20.00")
    branding.default_currency = "GBP"
    branding.save()

    invoice = create_invoice(
        user=user,
        line_items=[LineItemSpec(description="Hosting", quantity=Decimal("1"), unit_price=Decimal("100.00"))],
        source_kind=Invoice.SOURCE_MANUAL_ADMIN,
    )
    assert invoice.pk
    assert invoice.currency == "GBP"
    assert invoice.vat_rate == Decimal("20.00")
    assert invoice.subtotal == Decimal("100.00")
    assert invoice.vat_amount == Decimal("20.00")
    assert invoice.total == Decimal("120.00")
    assert invoice.due_date is not None
    assert invoice.line_items.count() == 1


@pytest.mark.django_db
def test_create_invoice_requires_at_least_one_line(django_user_model):
    from apps.billing.models import Invoice
    from apps.billing.services import create_invoice

    user = make_user(django_user_model)
    with pytest.raises(ValueError):
        create_invoice(user=user, line_items=[], source_kind=Invoice.SOURCE_MANUAL_ADMIN)


@pytest.mark.django_db
def test_mark_invoice_paid_sets_status_and_amount(django_user_model):
    from apps.billing.models import Invoice
    from apps.billing.services import LineItemSpec, create_invoice, mark_invoice_paid

    user = make_user(django_user_model)
    invoice = create_invoice(
        user=user,
        line_items=[LineItemSpec(description="X", quantity=Decimal("1"), unit_price=Decimal("50.00"))],
        source_kind=Invoice.SOURCE_MANUAL_ADMIN,
    )
    mark_invoice_paid(invoice, send_email=False)
    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_PAID
    assert invoice.amount_paid == invoice.total
    assert invoice.paid_at is not None


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_quote_sets_validity_from_branding():
    from apps.billing.models import BillingDocumentBranding, Quote
    from apps.billing.services import LineItemSpec, create_quote

    branding = BillingDocumentBranding.get_solo()
    branding.default_quote_validity_days = 7
    branding.save()

    quote = create_quote(
        line_items=[LineItemSpec(description="Audit", quantity=Decimal("1"), unit_price=Decimal("500"))],
        lead_email="prospect@example.com",
        lead_name="Pro Spect",
    )
    assert quote.status == Quote.STATUS_DRAFT
    assert quote.valid_until is not None
    delta = (quote.valid_until - timezone.now().date()).days
    assert 6 <= delta <= 7


@pytest.mark.django_db
def test_convert_quote_to_invoice_creates_invoice_and_marks_quote(django_user_model):
    from apps.billing.models import Invoice, Quote
    from apps.billing.services import LineItemSpec, convert_quote_to_invoice, create_quote

    user = make_user(django_user_model)
    quote = create_quote(
        user=user,
        line_items=[
            LineItemSpec(description="A", quantity=Decimal("2"), unit_price=Decimal("10")),
            LineItemSpec(description="B", quantity=Decimal("1"), unit_price=Decimal("30")),
        ],
        lead_email=user.email,
        lead_name="Test",
    )
    invoice = convert_quote_to_invoice(quote, by_user=user)
    quote.refresh_from_db()
    assert isinstance(invoice, Invoice)
    assert invoice.line_items.count() == 2
    assert quote.status == Quote.STATUS_CONVERTED
    assert quote.converted_invoice_id == invoice.pk


# ---------------------------------------------------------------------------
# Public quote builder
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_quote_builder_renders(client):
    from apps.products.models import Package

    Package.objects.create(
        name="Public Plan",
        slug="public-plan",
        price_monthly=Decimal("9.99"),
        price_annually=Decimal("99.00"),
        whm_package_name="x",
        is_active=True,
        is_quotable=True,
        quote_blurb="Great hosting",
        quote_category="Hosting",
    )
    response = client.get(reverse("billing_public:quote_builder"))
    assert response.status_code == 200
    assert b"Public Plan" in response.content
    assert b"Hosting" in response.content


@pytest.mark.django_db
def test_quote_submit_creates_quote_and_redirects(client, mailoutbox):
    from apps.billing.models import Quote

    payload = {
        "lead": {"name": "Alice", "email": "alice@example.com", "company": "Acme", "phone": "", "notes": ""},
        "items": [{"description": "Custom build", "quantity": 1, "unit_price": "1500.00"}],
        "hp": "",
    }
    response = client.post(
        reverse("billing_public:quote_submit"),
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["quote_number"]

    quote = Quote.objects.get(number=body["quote_number"])
    assert quote.lead_email == "alice@example.com"
    assert quote.line_items.count() == 1
    assert quote.status == Quote.STATUS_SENT


@pytest.mark.django_db
def test_quote_submit_honeypot_silently_succeeds(client):
    from apps.billing.models import Quote

    payload = {
        "lead": {"name": "Spammer", "email": "s@x.com"},
        "items": [{"description": "x", "quantity": 1, "unit_price": "1"}],
        "hp": "i-am-a-bot",
    }
    response = client.post(
        reverse("billing_public:quote_submit"),
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert Quote.objects.count() == 0


@pytest.mark.django_db
def test_quote_submit_rejects_empty_items(client):
    payload = {"lead": {"name": "A", "email": "a@b.c"}, "items": [], "hp": ""}
    response = client.post(
        reverse("billing_public:quote_submit"),
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_quote_public_view_marks_viewed(client):
    from apps.billing.models import Quote
    from apps.billing.services import LineItemSpec, create_quote

    quote = create_quote(
        line_items=[LineItemSpec(description="X", quantity=Decimal("1"), unit_price=Decimal("10"))],
        lead_email="x@y.com",
        lead_name="X Y",
        status=Quote.STATUS_SENT,
    )
    response = client.get(reverse("billing_public:quote_public", args=[str(quote.public_token)]))
    assert response.status_code == 200
    quote.refresh_from_db()
    assert quote.status == Quote.STATUS_VIEWED


@pytest.mark.django_db
def test_quote_public_accept_authenticated_creates_invoice(client, django_user_model):
    from apps.billing.models import Invoice, Quote
    from apps.billing.services import LineItemSpec, create_quote

    user = make_user(django_user_model)
    quote = create_quote(
        user=user,
        line_items=[LineItemSpec(description="X", quantity=Decimal("1"), unit_price=Decimal("10"))],
        lead_email=user.email,
        lead_name="X",
        status=Quote.STATUS_SENT,
    )
    client.force_login(user)
    response = client.post(reverse("billing_public:quote_public_accept", args=[str(quote.public_token)]))
    assert response.status_code == 302
    quote.refresh_from_db()
    assert quote.status == Quote.STATUS_CONVERTED
    assert Invoice.objects.filter(source_quote=quote).exists()


@pytest.mark.django_db
def test_quote_public_accept_anonymous_redirects_to_login(client):
    from apps.billing.models import Quote
    from apps.billing.services import LineItemSpec, create_quote

    quote = create_quote(
        line_items=[LineItemSpec(description="X", quantity=Decimal("1"), unit_price=Decimal("10"))],
        lead_email="x@y.com",
        lead_name="X",
        status=Quote.STATUS_SENT,
    )
    response = client.post(reverse("billing_public:quote_public_accept", args=[str(quote.public_token)]))
    assert response.status_code == 302
    # Login URL should include quote_token continuation
    assert "quote_token" in response.url or "login" in response.url.lower() or "next=" in response.url


# ---------------------------------------------------------------------------
# Quote expiry housekeeping task
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_expire_overdue_quotes_flips_status():
    from apps.billing.models import Quote
    from apps.billing.services import LineItemSpec, create_quote
    from apps.billing.tasks import expire_overdue_quotes

    fresh = create_quote(
        line_items=[LineItemSpec(description="A", quantity=Decimal("1"), unit_price=Decimal("1"))],
        lead_email="a@a.com",
        lead_name="A",
        status=Quote.STATUS_SENT,
    )
    stale = create_quote(
        line_items=[LineItemSpec(description="B", quantity=Decimal("1"), unit_price=Decimal("1"))],
        lead_email="b@b.com",
        lead_name="B",
        status=Quote.STATUS_SENT,
    )
    Quote.objects.filter(pk=stale.pk).update(valid_until=timezone.now().date() - timezone.timedelta(days=1))

    affected = expire_overdue_quotes()
    assert affected == 1
    fresh.refresh_from_db()
    stale.refresh_from_db()
    assert fresh.status == Quote.STATUS_SENT
    assert stale.status == Quote.STATUS_EXPIRED
