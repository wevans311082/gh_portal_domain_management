"""Canonical billing service.

Every part of the platform that creates an invoice or quote, or marks one
paid/void, MUST go through this module. That keeps numbering, branding
defaults, audit, downstream provisioning, and email behaviour identical
across domain registration, renewals, auto-renewals, Stripe webhooks,
quote acceptance, and the staff admin UI.

Public API:

* :func:`create_invoice` — atomically create an Invoice + line items.
* :func:`mark_invoice_paid` — flip to PAID, create payment, fire follow-ups.
* :func:`mark_invoice_void` — void with audit reason.
* :func:`create_quote` — atomically create a Quote + line items.
* :func:`convert_quote_to_invoice` — accept a quote, produce a draft invoice.
* :func:`email_document` — send an Invoice or Quote with PDF attached and
  an HTML preview embedded in the email body.
* :func:`render_invoice_pdf` / :func:`render_quote_pdf` — single source of
  truth for what a PDF looks like.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, List, Optional, Sequence

from django.db import transaction
from django.utils import timezone

from apps.billing.models import (
    BillingDocumentBranding,
    Invoice,
    InvoiceLineItem,
    Quote,
    QuoteLineItem,
)
from apps.billing.numbering import next_invoice_number, next_quote_number
from apps.billing.pdf import render_document_pdf

logger = logging.getLogger(__name__)


@dataclass
class LineItemSpec:
    """Lightweight DTO callers use to describe a single line item."""

    description: str
    unit_price: Decimal
    quantity: Decimal = Decimal("1")
    position: int = 0


def _coerce_line_items(items: Iterable) -> List[LineItemSpec]:
    out: List[LineItemSpec] = []
    for idx, item in enumerate(items):
        if isinstance(item, LineItemSpec):
            spec = item
        elif isinstance(item, dict):
            spec = LineItemSpec(
                description=str(item["description"]),
                unit_price=Decimal(str(item["unit_price"])),
                quantity=Decimal(str(item.get("quantity", 1))),
                position=int(item.get("position", idx)),
            )
        else:
            raise TypeError(f"Unsupported line item type: {type(item)!r}")
        if not spec.position:
            spec.position = idx
        out.append(spec)
    return out


def _resolve_billing_address(user) -> tuple[str, str]:
    """Return ``(billing_name, billing_address)`` for a user or empty strings."""
    if user is None:
        return "", ""

    name = getattr(user, "full_name", "") or ""
    if not name and hasattr(user, "get_full_name"):
        name = user.get_full_name() or ""
    name = (name or "").strip() or getattr(user, "email", "")
    address_parts: List[str] = []

    profile = getattr(user, "client_profile", None)
    if profile:
        for attr in ("address_line1", "address_line2", "city", "county", "postcode", "country"):
            val = getattr(profile, attr, "") or ""
            if val:
                address_parts.append(val)
    return name, "\n".join(address_parts)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@transaction.atomic
def create_invoice(
    *,
    user,
    line_items: Sequence,
    source_kind: str = Invoice.SOURCE_MANUAL_ADMIN,
    billing_name: Optional[str] = None,
    billing_address: Optional[str] = None,
    vat_rate: Optional[Decimal] = None,
    currency: Optional[str] = None,
    due_date=None,
    status: str = Invoice.STATUS_UNPAID,
    notes: str = "",
    source_quote: Optional[Quote] = None,
    created_by_staff=None,
    send_email: bool = False,
) -> Invoice:
    """Create an invoice through the unified pipeline.

    Pulls VAT %, currency, due date, and billing-name/address defaults from
    the branding singleton + user profile when the caller omits them.
    """
    branding = BillingDocumentBranding.get_solo()

    if vat_rate is None:
        vat_rate = branding.default_vat_rate
    if currency is None:
        currency = branding.default_currency
    if due_date is None and status != Invoice.STATUS_PAID:
        due_date = (
            timezone.now().date()
            + timezone.timedelta(days=branding.default_due_days)
        )

    if billing_name is None or billing_address is None:
        resolved_name, resolved_address = _resolve_billing_address(user)
        if billing_name is None:
            billing_name = resolved_name
        if billing_address is None:
            billing_address = resolved_address

    specs = _coerce_line_items(line_items)
    if not specs:
        raise ValueError("create_invoice requires at least one line item")

    paid_at = timezone.now() if status == Invoice.STATUS_PAID else None
    invoice = Invoice.objects.create(
        user=user,
        number=next_invoice_number(),
        status=status,
        source_kind=source_kind,
        currency=currency,
        vat_rate=vat_rate,
        due_date=due_date,
        notes=notes,
        billing_name=billing_name or "",
        billing_address=billing_address or "",
        source_quote=source_quote,
        created_by_staff=created_by_staff,
        paid_at=paid_at,
    )

    for spec in specs:
        InvoiceLineItem.objects.create(
            invoice=invoice,
            description=spec.description,
            quantity=spec.quantity,
            unit_price=spec.unit_price,
            position=spec.position,
        )

    invoice.calculate_totals()

    if status == Invoice.STATUS_PAID:
        invoice.amount_paid = invoice.total
        invoice.save(update_fields=["amount_paid"])

    logger.info(
        "Invoice %s created (source=%s, user=%s, total=%s %s)",
        invoice.number,
        source_kind,
        getattr(user, "email", user),
        invoice.total,
        currency,
    )

    if send_email:
        try:
            email_document(invoice, kind="invoice_issued")
        except Exception as exc:  # pragma: no cover - email is best-effort
            logger.exception("Failed to send invoice email for %s: %s", invoice.number, exc)

    return invoice


def _queue_paid_domain_orders(invoice: Invoice) -> None:
    """Trigger downstream provisioning when an invoice is paid.

    Lifted from ``apps.payments.views`` so every paid-invoice path (Stripe
    checkout, auto-renewal, manual admin "mark paid") fires the same
    follow-ups.
    """
    from apps.domains.models import DomainOrder, DomainRenewal
    from apps.domains import tasks as domain_tasks

    pending_orders = DomainOrder.objects.filter(
        invoice=invoice,
        status__in=[
            DomainOrder.STATUS_PENDING_PAYMENT,
            DomainOrder.STATUS_DRAFT,
            DomainOrder.STATUS_PAID,
        ],
    )
    for order in pending_orders:
        order.status = DomainOrder.STATUS_PAID
        order.save(update_fields=["status", "updated_at"])
        domain_tasks.register_domain_order.delay(order.id)

    pending_renewals = DomainRenewal.objects.filter(
        invoice=invoice,
        status=DomainRenewal.STATUS_PENDING_PAYMENT,
    )
    for renewal in pending_renewals:
        renewal.status = DomainRenewal.STATUS_PAID
        renewal.save(update_fields=["status"])
        domain_tasks.execute_domain_renewal.delay(renewal.id)


def mark_invoice_paid(
    invoice: Invoice,
    *,
    payment=None,
    paid_at=None,
    send_email: bool = True,
) -> Invoice:
    """Flip an invoice to PAID and run all downstream follow-ups."""
    paid_at = paid_at or timezone.now()
    invoice.status = Invoice.STATUS_PAID
    invoice.amount_paid = invoice.total
    invoice.paid_at = paid_at
    invoice.save(update_fields=["status", "amount_paid", "paid_at"])

    _queue_paid_domain_orders(invoice)

    if send_email:
        try:
            email_document(invoice, kind="invoice_paid")
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed to send invoice paid email: %s", exc)

    return invoice


def mark_invoice_void(invoice: Invoice, *, reason: str, by_user=None) -> Invoice:
    """Void an invoice and append the reason to the audit notes."""
    if invoice.status == Invoice.STATUS_PAID:
        raise ValueError("Cannot void a paid invoice; issue a credit note instead")

    invoice.status = Invoice.STATUS_VOID
    actor = getattr(by_user, "email", "system")
    note_line = f"[VOID by {actor} at {timezone.now():%Y-%m-%d %H:%M}] {reason}"
    invoice.notes = (invoice.notes + "\n" + note_line).strip()
    invoice.save(update_fields=["status", "notes"])
    return invoice


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


@transaction.atomic
def create_quote(
    *,
    user=None,
    line_items: Sequence,
    lead_email: str = "",
    lead_name: str = "",
    lead_company: str = "",
    lead_phone: str = "",
    vat_rate: Optional[Decimal] = None,
    currency: Optional[str] = None,
    valid_until=None,
    notes: str = "",
    internal_notes: str = "",
    status: str = Quote.STATUS_DRAFT,
    created_by=None,
) -> Quote:
    branding = BillingDocumentBranding.get_solo()
    if vat_rate is None:
        vat_rate = branding.default_vat_rate
    if currency is None:
        currency = branding.default_currency
    if valid_until is None:
        valid_until = (
            timezone.now().date()
            + timezone.timedelta(days=branding.default_quote_validity_days)
        )

    if user and not lead_email:
        lead_email = user.email
    if user and not lead_name:
        lead_name = (
            getattr(user, "full_name", "")
            or (user.get_full_name() if hasattr(user, "get_full_name") else "")
            or user.email
        )

    specs = _coerce_line_items(line_items)
    if not specs:
        raise ValueError("create_quote requires at least one line item")

    quote = Quote.objects.create(
        number=next_quote_number(),
        status=status,
        user=user,
        lead_email=lead_email,
        lead_name=lead_name,
        lead_company=lead_company,
        lead_phone=lead_phone,
        currency=currency,
        vat_rate=vat_rate,
        valid_until=valid_until,
        notes=notes,
        internal_notes=internal_notes,
        created_by=created_by,
    )

    for spec in specs:
        QuoteLineItem.objects.create(
            quote=quote,
            description=spec.description,
            quantity=spec.quantity,
            unit_price=spec.unit_price,
            position=spec.position,
        )

    quote.calculate_totals()
    logger.info("Quote %s created (user=%s)", quote.number, getattr(user, "email", "anon"))
    return quote


@transaction.atomic
def convert_quote_to_invoice(
    quote: Quote,
    *,
    by_user=None,
    accepted_ip: Optional[str] = None,
    send_email: bool = True,
) -> Invoice:
    """Convert an accepted/sent quote into a draft Invoice for payment."""
    if quote.converted_invoice is not None:
        return quote.converted_invoice
    if not quote.user:
        raise ValueError("Cannot convert a quote without a linked user account")

    line_items = [
        LineItemSpec(
            description=item.description,
            unit_price=item.unit_price,
            quantity=item.quantity,
            position=item.position,
        )
        for item in quote.line_items.all()
    ]

    invoice = create_invoice(
        user=quote.user,
        line_items=line_items,
        source_kind=Invoice.SOURCE_QUOTE_ACCEPTANCE,
        vat_rate=quote.vat_rate,
        currency=quote.currency,
        notes=f"Converted from quote {quote.number}",
        source_quote=quote,
        created_by_staff=by_user if by_user and by_user.is_staff else None,
        send_email=send_email,
    )

    quote.converted_invoice = invoice
    quote.status = Quote.STATUS_CONVERTED
    quote.accepted_at = quote.accepted_at or timezone.now()
    if accepted_ip:
        quote.accepted_by_ip = accepted_ip
    quote.save(
        update_fields=[
            "converted_invoice",
            "status",
            "accepted_at",
            "accepted_by_ip",
        ]
    )
    return invoice


# ---------------------------------------------------------------------------
# PDF + email
# ---------------------------------------------------------------------------


def _branding_context(branding: BillingDocumentBranding) -> dict:
    return {
        "branding": branding,
        "company_name": branding.company_name,
        "accent_colour": branding.accent_colour,
        "logo_url": branding.logo.url if branding.logo else "",
    }


def render_invoice_pdf(invoice: Invoice, *, base_url: Optional[str] = None):
    branding = BillingDocumentBranding.get_solo()
    ctx = {"invoice": invoice, "document": invoice, "doc_kind": "invoice"}
    ctx.update(_branding_context(branding))
    return render_document_pdf(
        "billing/document_pdf.html",
        ctx,
        header_template="billing/document_header.html",
        footer_template="billing/document_footer.html",
        base_url=base_url,
    )


def render_quote_pdf(quote: Quote, *, base_url: Optional[str] = None):
    branding = BillingDocumentBranding.get_solo()
    ctx = {"quote": quote, "document": quote, "doc_kind": "quote"}
    ctx.update(_branding_context(branding))
    return render_document_pdf(
        "billing/document_pdf.html",
        ctx,
        header_template="billing/document_header.html",
        footer_template="billing/document_footer.html",
        base_url=base_url,
    )


def email_document(
    document,
    *,
    kind: str,
    recipient_email: Optional[str] = None,
    cc: Optional[Sequence[str]] = None,
    custom_message: str = "",
) -> None:
    """Email an Invoice or Quote with a PDF attachment and HTML preview body.

    ``kind`` selects the notification template, e.g. ``invoice_issued``,
    ``invoice_paid``, ``quote_sent``, ``quote_accepted``.
    """
    from apps.notifications.services import send_notification

    branding = BillingDocumentBranding.get_solo()
    is_invoice = isinstance(document, Invoice)

    if is_invoice:
        pdf_bytes, content_type, ext = render_invoice_pdf(document)
        recipient = recipient_email or (document.user.email if document.user else "")
        number = document.number
        context = {
            "invoice": document,
            "invoice_number": number,
        }
    else:
        pdf_bytes, content_type, ext = render_quote_pdf(document)
        recipient = recipient_email or document.lead_email or (
            document.user.email if document.user else ""
        )
        number = document.number
        context = {
            "quote": document,
            "quote_number": number,
        }

    if not recipient:
        logger.warning("email_document: no recipient for %s", number)
        return

    context.update(
        {
            "document": document,
            "doc_kind": "invoice" if is_invoice else "quote",
            "custom_message": custom_message,
        }
    )
    context.update(_branding_context(branding))

    filename_prefix = "invoice" if is_invoice else "quote"
    attachments = [
        (f"{filename_prefix}-{number}.{ext}", pdf_bytes, content_type),
    ]

    user = document.user if document.user else _RecipientShim(recipient)

    send_notification(
        kind,
        user,
        context=context,
        attachments=attachments,
        recipient_email=recipient,
        cc=list(cc) if cc else None,
    )


class _RecipientShim:
    """Used when a Quote has no linked user but we still need to send mail."""

    def __init__(self, email: str):
        self.email = email

    def get_full_name(self):  # pragma: no cover - trivial
        return ""
