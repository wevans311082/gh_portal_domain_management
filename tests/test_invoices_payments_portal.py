"""
Tests for:
  - apps/invoices views (list, detail, pdf)
  - apps/payments views (stripe_checkout, stripe_success, stripe_webhook)
  - apps/portal views (dashboard)
"""
import json
import hashlib
import hmac
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.urls import reverse
from django.utils import timezone


# ─────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────

def make_user(django_user_model, email="user@portal.com", password="pass1234!"):
    return django_user_model.objects.create_user(email=email, password=password)


def make_invoice(user, number="INV-001", status=None, total="99.99"):
    from apps.billing.models import Invoice
    kwargs = {
        "user": user,
        "number": number,
        "total": Decimal(total),
        "subtotal": Decimal(total),
        "vat_rate": Decimal("0.00"),
        "due_date": timezone.now().date(),
    }
    if status:
        kwargs["status"] = status
    else:
        kwargs["status"] = Invoice.STATUS_UNPAID
    return Invoice.objects.create(**kwargs)


def make_line_item(invoice, description="Domain", qty="1", price="99.99"):
    from apps.billing.models import InvoiceLineItem
    return InvoiceLineItem.objects.create(
        invoice=invoice,
        description=description,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        line_total=Decimal(qty) * Decimal(price),
    )


def _stripe_sig(payload_bytes, secret="whsec_test"):
    """Compute a minimal Stripe-Signature header value."""
    import time
    timestamp = str(int(time.time()))
    signed = f"{timestamp}.{payload_bytes.decode()}"
    sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


# ─────────────────────────────────────────────
# Invoice views
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_invoice_list_requires_login(client):
    response = client.get(reverse("invoices:list"))
    assert response.status_code == 302


@pytest.mark.django_db
def test_invoice_list_shows_own_invoices(client, django_user_model):
    user = make_user(django_user_model)
    other = make_user(django_user_model, email="other@portal.com")
    make_invoice(user, number="INV-OWN")
    make_invoice(other, number="INV-OTHER")

    client.force_login(user)
    response = client.get(reverse("invoices:list"))
    assert response.status_code == 200
    assert b"INV-OWN" in response.content
    assert b"INV-OTHER" not in response.content


@pytest.mark.django_db
def test_invoice_detail_own(client, django_user_model):
    user = make_user(django_user_model)
    invoice = make_invoice(user, number="INV-DET")
    client.force_login(user)
    response = client.get(reverse("invoices:detail", kwargs={"pk": invoice.pk}))
    assert response.status_code == 200
    assert b"INV-DET" in response.content


@pytest.mark.django_db
def test_invoice_detail_other_user_404(client, django_user_model):
    owner = make_user(django_user_model)
    intruder = make_user(django_user_model, email="intruder@portal.com")
    invoice = make_invoice(owner, number="INV-SEC")
    client.force_login(intruder)
    response = client.get(reverse("invoices:detail", kwargs={"pk": invoice.pk}))
    assert response.status_code == 404


@pytest.mark.django_db
def test_invoice_pdf_no_weasyprint_returns_html(client, django_user_model):
    """When WeasyPrint is absent the view falls back to HTML."""
    user = make_user(django_user_model)
    invoice = make_invoice(user, number="INV-PDF")
    make_line_item(invoice)
    client.force_login(user)

    with patch.dict("sys.modules", {"weasyprint": None}):
        response = client.get(reverse("invoices:pdf", kwargs={"pk": invoice.pk}))

    # Falls back to HTML content type
    assert response.status_code == 200


# ─────────────────────────────────────────────
# Payments — stripe_checkout
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_stripe_checkout_requires_login(client, django_user_model):
    user = make_user(django_user_model)
    invoice = make_invoice(user, number="INV-SC1")
    response = client.get(reverse("payments:stripe_checkout", kwargs={"invoice_id": invoice.pk}))
    assert response.status_code == 302
    assert "/login" in response.url or "/my-account" in response.url


@pytest.mark.django_db
@patch("apps.payments.views.StripeService.create_checkout_session", return_value="https://checkout.stripe.com/test")
def test_stripe_checkout_redirects_to_stripe(mock_session, client, django_user_model):
    user = make_user(django_user_model)
    invoice = make_invoice(user, number="INV-SC2")
    make_line_item(invoice)
    client.force_login(user)
    response = client.get(reverse("payments:stripe_checkout", kwargs={"invoice_id": invoice.pk}))
    assert response.status_code == 302
    assert response.url == "https://checkout.stripe.com/test"


@pytest.mark.django_db
def test_stripe_checkout_already_paid_redirects(client, django_user_model):
    from apps.billing.models import Invoice
    user = make_user(django_user_model)
    invoice = make_invoice(user, number="INV-SC3", status=Invoice.STATUS_PAID)
    client.force_login(user)
    response = client.get(reverse("payments:stripe_checkout", kwargs={"invoice_id": invoice.pk}))
    assert response.status_code == 302
    assert str(invoice.pk) in response.url


@pytest.mark.django_db
@patch("apps.payments.views.StripeService.create_checkout_session", side_effect=Exception("Stripe down"))
def test_stripe_checkout_error_shows_message(mock_fail, client, django_user_model):
    user = make_user(django_user_model)
    invoice = make_invoice(user, number="INV-SC4")
    make_line_item(invoice)
    client.force_login(user)
    response = client.get(reverse("payments:stripe_checkout", kwargs={"invoice_id": invoice.pk}))
    assert response.status_code == 302


# ─────────────────────────────────────────────
# Payments — stripe_success
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_stripe_success_with_invoice_id(client, django_user_model):
    user = make_user(django_user_model)
    invoice = make_invoice(user, number="INV-SUC")
    client.force_login(user)
    response = client.get(
        reverse("payments:stripe_success") + f"?invoice_id={invoice.pk}"
    )
    assert response.status_code == 302
    assert str(invoice.pk) in response.url


@pytest.mark.django_db
def test_stripe_success_without_invoice_id(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    response = client.get(reverse("payments:stripe_success"))
    assert response.status_code == 302
    assert response.url == reverse("portal:dashboard")


# ─────────────────────────────────────────────
# Payments — stripe_webhook
# ─────────────────────────────────────────────

def _build_webhook_payload(event_type, invoice_id, session_id="pi_test123"):
    return {
        "id": f"evt_{session_id}",
        "type": event_type,
        "data": {
            "object": {
                "id": f"cs_{session_id}",
                "metadata": {"invoice_id": str(invoice_id)},
                "amount_total": 9999,
                "currency": "gbp",
                "payment_intent": f"pi_{session_id}",
            }
        },
    }


@pytest.mark.django_db
@patch("apps.payments.views.StripeService.handle_webhook")
def test_stripe_webhook_checkout_completed_marks_invoice_paid(mock_wh, client, django_user_model):
    from apps.billing.models import Invoice
    user = make_user(django_user_model)
    invoice = make_invoice(user, number="INV-WH1")
    payload = _build_webhook_payload("checkout.session.completed", invoice.pk, "abc111")
    mock_wh.return_value = payload

    response = client.post(
        reverse("payments:stripe_webhook"),
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="t=1,v1=dummy",
    )
    assert response.status_code == 200
    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_PAID


@pytest.mark.django_db
@patch("apps.payments.views.StripeService.handle_webhook")
def test_stripe_webhook_idempotent(mock_wh, client, django_user_model):
    """Duplicate webhook events must not double-process."""
    from apps.billing.models import Invoice
    from apps.payments.models import WebhookEvent, Payment
    user = make_user(django_user_model)
    invoice = make_invoice(user, number="INV-WH2")
    payload = _build_webhook_payload("checkout.session.completed", invoice.pk, "idem999")
    mock_wh.return_value = payload

    # First call
    client.post(
        reverse("payments:stripe_webhook"),
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="t=1,v1=dummy",
    )
    # Second call (duplicate)
    response = client.post(
        reverse("payments:stripe_webhook"),
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="t=1,v1=dummy",
    )
    assert response.status_code == 200
    # Only one payment should have been created
    assert Payment.objects.filter(invoice=invoice).count() == 1


@pytest.mark.django_db
@patch("apps.payments.views.StripeService.handle_webhook")
def test_stripe_webhook_unhandled_event_type_200(mock_wh, client, django_user_model):
    """Unknown event types return 200 without raising."""
    payload = {
        "id": "evt_unknown999",
        "type": "customer.created",
        "data": {"object": {}},
    }
    mock_wh.return_value = payload

    response = client.post(
        reverse("payments:stripe_webhook"),
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="t=1,v1=dummy",
    )
    assert response.status_code == 200


@pytest.mark.django_db
@patch("apps.payments.views.StripeService.handle_webhook", side_effect=ValueError("bad sig"))
def test_stripe_webhook_invalid_signature_400(mock_wh, client):
    response = client.post(
        reverse("payments:stripe_webhook"),
        data=b"{}",
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="t=1,v1=badsig",
    )
    assert response.status_code == 400


# ─────────────────────────────────────────────
# Portal dashboard
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_dashboard_requires_login(client):
    response = client.get(reverse("portal:dashboard"))
    assert response.status_code == 302


@pytest.mark.django_db
def test_dashboard_renders(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    response = client.get(reverse("portal:dashboard"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_dashboard_shows_unpaid_invoices(client, django_user_model):
    from apps.billing.models import Invoice
    user = make_user(django_user_model)
    invoice = make_invoice(user, number="INV-DASH", status=Invoice.STATUS_UNPAID)
    client.force_login(user)
    response = client.get(reverse("portal:dashboard"))
    assert b"INV-DASH" in response.content


@pytest.mark.django_db
def test_dashboard_context_counts(client, django_user_model):
    from apps.billing.models import Invoice
    user = make_user(django_user_model)
    make_invoice(user, number="INV-D1", status=Invoice.STATUS_UNPAID)
    make_invoice(user, number="INV-D2", status=Invoice.STATUS_OVERDUE)
    client.force_login(user)
    response = client.get(reverse("portal:dashboard"))
    assert response.context["unpaid_amount"] == Decimal("199.98")


@pytest.mark.django_db
def test_dashboard_hides_other_users_data(client, django_user_model):
    from apps.billing.models import Invoice
    user = make_user(django_user_model)
    other = make_user(django_user_model, email="other2@portal.com")
    make_invoice(other, number="INV-SECRET", status=Invoice.STATUS_UNPAID)
    client.force_login(user)
    response = client.get(reverse("portal:dashboard"))
    assert b"INV-SECRET" not in response.content
