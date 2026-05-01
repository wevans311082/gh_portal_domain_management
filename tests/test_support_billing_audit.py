"""
Tests for:
  - apps/support models (SupportTicket, SupportTicketMessage, attachment validation)
  - apps/support views (ticket_list, ticket_create, ticket_detail / reply)
  - apps/billing models (Invoice, InvoiceLineItem calculate_totals, amount_outstanding)
  - apps/audit middleware (AuditLogMiddleware)
"""
import io
import pytest
from decimal import Decimal
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def make_user(django_user_model, email="user@support.com", password="pass1234!"):
    return django_user_model.objects.create_user(email=email, password=password)


def make_ticket(user, subject="Test issue", status=None):
    from apps.support.models import SupportTicket
    kwargs = {"user": user, "subject": subject}
    if status:
        kwargs["status"] = status
    return SupportTicket.objects.create(**kwargs)


# ─────────────────────────────────────────────
# Support model — attachment validation
# ─────────────────────────────────────────────

def test_allowed_attachment_extension_passes(django_user_model):
    """_validate_attachment should not raise for a .pdf under 5 MB."""
    from apps.support.models import _validate_attachment
    from django.core.exceptions import ValidationError

    fake_pdf = SimpleUploadedFile("doc.pdf", b"x" * 100, content_type="application/pdf")
    # Should not raise
    _validate_attachment(fake_pdf)


def test_disallowed_attachment_extension_raises():
    from apps.support.models import _validate_attachment
    from django.core.exceptions import ValidationError

    fake_exe = SimpleUploadedFile("evil.exe", b"x", content_type="application/octet-stream")
    with pytest.raises(ValidationError, match="not permitted"):
        _validate_attachment(fake_exe)


def test_oversized_attachment_raises():
    from apps.support.models import _validate_attachment
    from django.core.exceptions import ValidationError

    big_file = SimpleUploadedFile("huge.txt", b"x" * (6 * 1024 * 1024), content_type="text/plain")
    with pytest.raises(ValidationError, match="smaller than"):
        _validate_attachment(big_file)


# ─────────────────────────────────────────────
# Support views — ticket_list
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_ticket_list_requires_login(client):
    response = client.get(reverse("support:list"))
    assert response.status_code == 302


@pytest.mark.django_db
def test_ticket_list_shows_own_tickets(client, django_user_model):
    user = make_user(django_user_model)
    other = make_user(django_user_model, email="other@support.com")
    make_ticket(user, subject="My ticket")
    make_ticket(other, subject="Other ticket")

    client.force_login(user)
    response = client.get(reverse("support:list"))
    assert response.status_code == 200
    assert b"My ticket" in response.content
    assert b"Other ticket" not in response.content


# ─────────────────────────────────────────────
# Support views — ticket_create
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_ticket_create_get(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    response = client.get(reverse("support:create"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_ticket_create_post_creates_ticket_and_message(client, django_user_model):
    from apps.support.models import SupportTicket, SupportTicketMessage
    user = make_user(django_user_model)
    client.force_login(user)

    response = client.post(reverse("support:create"), {
        "subject": "Help me",
        "priority": "normal",
        "message": "I need help badly.",
    })
    assert response.status_code == 302
    ticket = SupportTicket.objects.get(subject="Help me")
    assert ticket.user == user
    assert SupportTicketMessage.objects.filter(ticket=ticket).count() == 1


@pytest.mark.django_db
def test_ticket_create_invalid_form_stays_on_page(client, django_user_model):
    """message is required, so missing it keeps the user on the create page."""
    user = make_user(django_user_model)
    client.force_login(user)
    response = client.post(reverse("support:create"), {
        "subject": "Silent ticket",
        "priority": "low",
        # 'message' intentionally omitted — form should be invalid
    })
    assert response.status_code == 200  # stays on page


# ─────────────────────────────────────────────
# Support views — ticket_detail
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_ticket_detail_get(client, django_user_model):
    user = make_user(django_user_model)
    ticket = make_ticket(user)
    client.force_login(user)
    response = client.get(reverse("support:detail", kwargs={"pk": ticket.pk}))
    assert response.status_code == 200
    assert ticket.subject.encode() in response.content


@pytest.mark.django_db
def test_ticket_detail_other_user_404(client, django_user_model):
    owner = make_user(django_user_model)
    intruder = make_user(django_user_model, email="intruder@support.com")
    ticket = make_ticket(owner)
    client.force_login(intruder)
    response = client.get(reverse("support:detail", kwargs={"pk": ticket.pk}))
    assert response.status_code == 404


@pytest.mark.django_db
def test_ticket_reply_adds_message_and_updates_status(client, django_user_model):
    from apps.support.models import SupportTicket, SupportTicketMessage
    user = make_user(django_user_model)
    ticket = make_ticket(user, status=SupportTicket.STATUS_OPEN)
    client.force_login(user)

    response = client.post(
        reverse("support:detail", kwargs={"pk": ticket.pk}),
        {"content": "Please hurry!"},
    )
    assert response.status_code == 302
    ticket.refresh_from_db()
    assert ticket.status == SupportTicket.STATUS_AWAITING_SUPPORT
    assert SupportTicketMessage.objects.filter(ticket=ticket).count() == 1


# ─────────────────────────────────────────────
# Billing models
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_invoice_line_item_auto_calculates_total(django_user_model):
    from apps.billing.models import Invoice, InvoiceLineItem
    user = make_user(django_user_model)
    invoice = Invoice.objects.create(
        user=user,
        number="INV-T001",
        status=Invoice.STATUS_DRAFT,
        vat_rate=Decimal("0.00"),
        due_date=None,
    )
    item = InvoiceLineItem.objects.create(
        invoice=invoice,
        description="Widget",
        quantity=Decimal("3"),
        unit_price=Decimal("5.00"),
        line_total=Decimal("0.00"),  # will be overridden by save()
    )
    assert item.line_total == Decimal("15.00")


@pytest.mark.django_db
def test_invoice_calculate_totals(django_user_model):
    from apps.billing.models import Invoice, InvoiceLineItem
    user = make_user(django_user_model, email="billing@test.com")
    invoice = Invoice.objects.create(
        user=user,
        number="INV-T002",
        status=Invoice.STATUS_DRAFT,
        vat_rate=Decimal("20.00"),
        due_date=None,
    )
    InvoiceLineItem.objects.create(
        invoice=invoice,
        description="Service A",
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
        line_total=Decimal("100.00"),
    )
    InvoiceLineItem.objects.create(
        invoice=invoice,
        description="Service B",
        quantity=Decimal("2"),
        unit_price=Decimal("50.00"),
        line_total=Decimal("100.00"),
    )
    invoice.calculate_totals()
    invoice.refresh_from_db()
    assert invoice.subtotal == Decimal("200.00")
    assert invoice.vat_amount == Decimal("40.00")
    assert invoice.total == Decimal("240.00")


@pytest.mark.django_db
def test_invoice_amount_outstanding(django_user_model):
    from apps.billing.models import Invoice
    user = make_user(django_user_model, email="outstanding@test.com")
    invoice = Invoice.objects.create(
        user=user,
        number="INV-T003",
        status=Invoice.STATUS_UNPAID,
        total=Decimal("120.00"),
        amount_paid=Decimal("50.00"),
        vat_rate=Decimal("0.00"),
    )
    assert invoice.amount_outstanding == Decimal("70.00")


@pytest.mark.django_db
def test_invoice_str(django_user_model):
    from apps.billing.models import Invoice
    user = make_user(django_user_model, email="str@test.com")
    invoice = Invoice.objects.create(
        user=user,
        number="INV-STR",
        status=Invoice.STATUS_DRAFT,
        vat_rate=Decimal("0.00"),
    )
    assert str(invoice) == "Invoice #INV-STR"


# ─────────────────────────────────────────────
# Audit middleware
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_audit_middleware_creates_log_for_post(client, django_user_model):
    from apps.audit.models import AuditLog
    user = make_user(django_user_model)
    client.force_login(user)
    # POST to any authenticated endpoint that takes POST, e.g. support ticket create
    client.post(reverse("support:create"), {
        "subject": "Audit test",
        "priority": "normal",
        "message": "audit me",
    })
    assert AuditLog.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_audit_middleware_skips_get(client, django_user_model):
    from apps.audit.models import AuditLog
    user = make_user(django_user_model)
    client.force_login(user)
    AuditLog.objects.filter(user=user).delete()
    client.get(reverse("support:list"))
    assert AuditLog.objects.filter(user=user).count() == 0


@pytest.mark.django_db
def test_audit_middleware_skips_anonymous_post(client):
    from apps.audit.models import AuditLog
    before = AuditLog.objects.count()
    client.post(reverse("accounts_custom:login"), {"email": "x", "password": "y"})
    after = AuditLog.objects.count()
    assert after == before
