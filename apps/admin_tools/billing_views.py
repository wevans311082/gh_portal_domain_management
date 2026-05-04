"""Staff-facing billing UI: invoice & quote CRUD, branding settings.

These views live in ``apps.admin_tools`` because that's where the staff
"workbench" lives, but every persistence call routes through
``apps.billing.services`` so behaviour matches the canonical pipeline.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import List

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.billing.models import (
    BillingDocumentBranding,
    Invoice,
    InvoiceLineItem,
    Quote,
    QuoteLineItem,
)
from apps.domains.models import Domain, TLDPricing
from apps.billing.services import (
    LineItemSpec,
    convert_quote_to_invoice,
    create_invoice,
    create_quote,
    email_document,
    mark_invoice_paid,
    mark_invoice_void,
    render_invoice_pdf,
    render_quote_pdf,
)
from apps.portal.cart_service import (
    add_domain_registration_item,
    add_domain_renewal_item,
    add_domain_transfer_item,
    add_hosting_item,
    create_invoice_from_cart,
    create_quote_from_cart,
    get_active_cart,
    remove_cart_item,
)
from .decorators import staff_member_required
from apps.products.models import Package


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decimal(value, default=None):
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _parse_line_items(post) -> List[LineItemSpec]:
    descriptions = post.getlist("line_description[]")
    quantities = post.getlist("line_quantity[]")
    unit_prices = post.getlist("line_unit_price[]")
    positions = post.getlist("line_position[]")

    items: List[LineItemSpec] = []
    for idx, description in enumerate(descriptions):
        description = (description or "").strip()
        if not description:
            continue
        qty = _decimal(quantities[idx] if idx < len(quantities) else None, Decimal("1"))
        price = _decimal(unit_prices[idx] if idx < len(unit_prices) else None, Decimal("0"))
        try:
            position = int(positions[idx]) if idx < len(positions) and positions[idx] else idx
        except ValueError:
            position = idx
        items.append(
            LineItemSpec(
                description=description,
                quantity=qty,
                unit_price=price,
                position=position,
            )
        )
    return items


def _resolve_user(post):
    """Resolve the User for an invoice from a posted user_id."""
    user_id = post.get("user_id")
    if not user_id:
        return None
    return User.objects.filter(pk=user_id).first()


def _resolve_builder_user(request):
    user_id = request.GET.get("user_id") or request.POST.get("user_id")
    if not user_id:
        return None
    return User.objects.filter(pk=user_id).first()


# ---------------------------------------------------------------------------
# Branding singleton
# ---------------------------------------------------------------------------


@staff_member_required
def branding_edit(request):
    branding = BillingDocumentBranding.get_solo()

    if request.method == "POST":
        for field in [
            "company_name",
            "registered_address",
            "company_number",
            "vat_number",
            "support_email",
            "support_phone",
            "website_url",
            "accent_colour",
            "header_text",
            "footer_text",
            "legal_text",
            "signature_block",
            "default_currency",
            "invoice_number_format",
            "quote_number_format",
        ]:
            if field in request.POST:
                setattr(branding, field, request.POST.get(field, "").strip())

        for dec_field in ("default_vat_rate",):
            val = _decimal(request.POST.get(dec_field))
            if val is not None:
                setattr(branding, dec_field, val)

        for int_field in ("default_due_days", "default_quote_validity_days"):
            try:
                val = int(request.POST.get(int_field, ""))
                setattr(branding, int_field, max(0, val))
            except (TypeError, ValueError):
                pass

        if request.FILES.get("logo"):
            branding.logo = request.FILES["logo"]
        if request.POST.get("clear_logo") == "1" and branding.logo:
            branding.logo.delete(save=False)
            branding.logo = None

        branding.save()
        messages.success(request, "Billing document branding saved.")
        return redirect("admin_tools:billing_branding")

    return render(
        request,
        "admin_tools/billing/branding.html",
        {"branding": branding},
    )


# ---------------------------------------------------------------------------
# Invoices: list / create / edit / actions
# ---------------------------------------------------------------------------


@staff_member_required
def invoice_list(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    qs = Invoice.objects.select_related("user").order_by("-created_at")
    if q:
        qs = qs.filter(
            Q(number__icontains=q)
            | Q(user__email__icontains=q)
            | Q(billing_name__icontains=q)
        )
    if status:
        qs = qs.filter(status=status)

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "admin_tools/billing/invoice_list.html",
        {
            "page_obj": page_obj,
            "invoices": page_obj.object_list,
            "search_q": q,
            "status_filter": status,
            "status_choices": Invoice.STATUS_CHOICES,
        },
    )


@staff_member_required
def invoice_create(request):
    branding = BillingDocumentBranding.get_solo()

    if request.method == "POST":
        user = _resolve_user(request.POST)
        if not user:
            messages.error(request, "Pick a customer for this invoice.")
            return redirect("admin_tools:invoice_create")

        line_items = _parse_line_items(request.POST)
        if not line_items:
            messages.error(request, "Add at least one line item.")
            return redirect("admin_tools:invoice_create")

        vat_rate = _decimal(request.POST.get("vat_rate"), branding.default_vat_rate)
        currency = (request.POST.get("currency") or branding.default_currency).strip()
        notes = (request.POST.get("notes") or "").strip()
        due_date_raw = (request.POST.get("due_date") or "").strip()
        due_date = None
        if due_date_raw:
            try:
                due_date = timezone.datetime.strptime(due_date_raw, "%Y-%m-%d").date()
            except ValueError:
                due_date = None

        status = (request.POST.get("status") or Invoice.STATUS_DRAFT).strip()
        send_email = request.POST.get("send_email") == "1"

        invoice = create_invoice(
            user=user,
            line_items=line_items,
            source_kind=Invoice.SOURCE_MANUAL_ADMIN,
            vat_rate=vat_rate,
            currency=currency,
            due_date=due_date,
            status=status,
            notes=notes,
            created_by_staff=request.user,
            send_email=send_email,
        )
        messages.success(request, f"Invoice {invoice.number} created.")
        return redirect("admin_tools:invoice_edit", pk=invoice.pk)

    users = User.objects.order_by("email")[:200]
    return render(
        request,
        "admin_tools/billing/invoice_form.html",
        {
            "invoice": None,
            "branding": branding,
            "users": users,
            "status_choices": Invoice.STATUS_CHOICES,
            "form_action": reverse("admin_tools:invoice_create"),
        },
    )


@staff_member_required
def invoice_edit(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    branding = BillingDocumentBranding.get_solo()

    if request.method == "POST":
        if not invoice.is_editable:
            messages.error(request, "This invoice is locked and cannot be edited.")
            return redirect("admin_tools:invoice_edit", pk=invoice.pk)

        line_items = _parse_line_items(request.POST)
        if not line_items:
            messages.error(request, "Add at least one line item.")
            return redirect("admin_tools:invoice_edit", pk=invoice.pk)

        invoice.notes = (request.POST.get("notes") or "").strip()
        invoice.billing_name = (request.POST.get("billing_name") or "").strip()
        invoice.billing_address = (request.POST.get("billing_address") or "").strip()
        vat_rate = _decimal(request.POST.get("vat_rate"))
        if vat_rate is not None:
            invoice.vat_rate = vat_rate
        currency = (request.POST.get("currency") or "").strip()
        if currency:
            invoice.currency = currency
        due_date_raw = (request.POST.get("due_date") or "").strip()
        if due_date_raw:
            try:
                invoice.due_date = timezone.datetime.strptime(due_date_raw, "%Y-%m-%d").date()
            except ValueError:
                pass
        new_status = (request.POST.get("status") or "").strip()
        # Only allow safe status transitions via the edit form.
        # PAID and VOID must go through invoice_action (mark_paid / void)
        # so that paid_at, amount_paid, and provisioning queuing are handled.
        _EDITABLE_STATUSES = {Invoice.STATUS_DRAFT, Invoice.STATUS_UNPAID, Invoice.STATUS_OVERDUE}
        if new_status and new_status in _EDITABLE_STATUSES:
            invoice.status = new_status
        invoice.save()

        # Replace line items wholesale.
        InvoiceLineItem.objects.filter(invoice=invoice).delete()
        for spec in line_items:
            InvoiceLineItem.objects.create(
                invoice=invoice,
                description=spec.description,
                quantity=spec.quantity,
                unit_price=spec.unit_price,
                position=spec.position,
            )
        invoice.calculate_totals()
        messages.success(request, "Invoice updated.")
        return redirect("admin_tools:invoice_edit", pk=invoice.pk)

    return render(
        request,
        "admin_tools/billing/invoice_form.html",
        {
            "invoice": invoice,
            "line_items": invoice.line_items.all(),
            "branding": branding,
            "status_choices": Invoice.STATUS_CHOICES,
            "form_action": reverse("admin_tools:invoice_edit", args=[invoice.pk]),
        },
    )


@staff_member_required
@require_POST
def invoice_action(request, pk, action):
    invoice = get_object_or_404(Invoice, pk=pk)

    if action == "send":
        recipient = (request.POST.get("recipient") or "").strip() or None
        custom_message = (request.POST.get("message") or "").strip()
        try:
            email_document(
                invoice,
                kind="invoice_issued",
                recipient_email=recipient,
                custom_message=custom_message,
            )
            messages.success(request, f"Invoice {invoice.number} emailed.")
        except Exception as exc:
            messages.error(request, f"Email failed: {exc}")
    elif action == "mark_paid":
        mark_invoice_paid(invoice)
        messages.success(request, f"Invoice {invoice.number} marked paid.")
    elif action == "void":
        try:
            mark_invoice_void(
                invoice,
                reason=request.POST.get("reason") or "Voided by staff",
                by_user=request.user,
            )
            messages.success(request, f"Invoice {invoice.number} voided.")
        except ValueError as exc:
            messages.error(request, str(exc))
    elif action == "delete":
        if invoice.status != Invoice.STATUS_DRAFT:
            messages.error(request, "Only draft invoices can be deleted.")
        else:
            number = invoice.number
            invoice.delete()
            messages.success(request, f"Invoice {number} deleted.")
            return redirect("admin_tools:invoice_list")
    else:
        messages.error(request, f"Unknown action: {action}")

    return redirect("admin_tools:invoice_edit", pk=invoice.pk)


@staff_member_required
def invoice_pdf(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    pdf_bytes, content_type, ext = render_invoice_pdf(
        invoice, base_url=request.build_absolute_uri("/")
    )
    disposition = "inline" if request.GET.get("inline") == "1" else "attachment"
    response = HttpResponse(pdf_bytes, content_type=content_type)
    response["Content-Disposition"] = (
        f'{disposition}; filename="invoice-{invoice.number}.{ext}"'
    )
    return response


# ---------------------------------------------------------------------------
# Quotes: list / create / edit / actions
# ---------------------------------------------------------------------------


@staff_member_required
def quote_list(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    qs = Quote.objects.select_related("user").order_by("-created_at")
    if q:
        qs = qs.filter(
            Q(number__icontains=q)
            | Q(user__email__icontains=q)
            | Q(lead_email__icontains=q)
            | Q(lead_name__icontains=q)
            | Q(lead_company__icontains=q)
        )
    if status:
        qs = qs.filter(status=status)

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "admin_tools/billing/quote_list.html",
        {
            "page_obj": page_obj,
            "quotes": page_obj.object_list,
            "search_q": q,
            "status_filter": status,
            "status_choices": Quote.STATUS_CHOICES,
        },
    )


@staff_member_required
def quote_create(request):
    branding = BillingDocumentBranding.get_solo()

    if request.method == "POST":
        line_items = _parse_line_items(request.POST)
        if not line_items:
            messages.error(request, "Add at least one line item.")
            return redirect("admin_tools:quote_create")

        user = _resolve_user(request.POST)
        vat_rate = _decimal(request.POST.get("vat_rate"), branding.default_vat_rate)
        currency = (request.POST.get("currency") or branding.default_currency).strip()
        valid_until_raw = (request.POST.get("valid_until") or "").strip()
        valid_until = None
        if valid_until_raw:
            try:
                valid_until = timezone.datetime.strptime(valid_until_raw, "%Y-%m-%d").date()
            except ValueError:
                valid_until = None

        quote = create_quote(
            user=user,
            line_items=line_items,
            lead_email=(request.POST.get("lead_email") or "").strip(),
            lead_name=(request.POST.get("lead_name") or "").strip(),
            lead_company=(request.POST.get("lead_company") or "").strip(),
            lead_phone=(request.POST.get("lead_phone") or "").strip(),
            vat_rate=vat_rate,
            currency=currency,
            valid_until=valid_until,
            notes=(request.POST.get("notes") or "").strip(),
            internal_notes=(request.POST.get("internal_notes") or "").strip(),
            status=Quote.STATUS_DRAFT,
            created_by=request.user,
        )
        messages.success(request, f"Quote {quote.number} created.")
        return redirect("admin_tools:quote_edit", pk=quote.pk)

    users = User.objects.order_by("email")[:200]
    return render(
        request,
        "admin_tools/billing/quote_form.html",
        {
            "quote": None,
            "branding": branding,
            "users": users,
            "status_choices": Quote.STATUS_CHOICES,
            "form_action": reverse("admin_tools:quote_create"),
        },
    )


@staff_member_required
def quote_edit(request, pk):
    quote = get_object_or_404(Quote, pk=pk)
    branding = BillingDocumentBranding.get_solo()

    if request.method == "POST":
        if not quote.is_editable:
            messages.error(request, "This quote is locked and cannot be edited.")
            return redirect("admin_tools:quote_edit", pk=quote.pk)

        line_items = _parse_line_items(request.POST)
        if not line_items:
            messages.error(request, "Add at least one line item.")
            return redirect("admin_tools:quote_edit", pk=quote.pk)

        for field in ("lead_email", "lead_name", "lead_company", "lead_phone", "notes", "internal_notes"):
            if field in request.POST:
                setattr(quote, field, (request.POST.get(field) or "").strip())

        vat_rate = _decimal(request.POST.get("vat_rate"))
        if vat_rate is not None:
            quote.vat_rate = vat_rate
        currency = (request.POST.get("currency") or "").strip()
        if currency:
            quote.currency = currency
        valid_until_raw = (request.POST.get("valid_until") or "").strip()
        if valid_until_raw:
            try:
                quote.valid_until = timezone.datetime.strptime(valid_until_raw, "%Y-%m-%d").date()
            except ValueError:
                pass
        new_status = (request.POST.get("status") or "").strip()
        if new_status and new_status in dict(Quote.STATUS_CHOICES):
            quote.status = new_status
        quote.save()

        QuoteLineItem.objects.filter(quote=quote).delete()
        for spec in line_items:
            QuoteLineItem.objects.create(
                quote=quote,
                description=spec.description,
                quantity=spec.quantity,
                unit_price=spec.unit_price,
                position=spec.position,
            )
        quote.calculate_totals()
        messages.success(request, "Quote updated.")
        return redirect("admin_tools:quote_edit", pk=quote.pk)

    return render(
        request,
        "admin_tools/billing/quote_form.html",
        {
            "quote": quote,
            "line_items": quote.line_items.all(),
            "branding": branding,
            "status_choices": Quote.STATUS_CHOICES,
            "form_action": reverse("admin_tools:quote_edit", args=[quote.pk]),
        },
    )


@staff_member_required
@require_POST
def quote_action(request, pk, action):
    quote = get_object_or_404(Quote, pk=pk)

    if action == "send":
        recipient = (request.POST.get("recipient") or "").strip() or None
        custom_message = (request.POST.get("message") or "").strip()
        try:
            email_document(
                quote,
                kind="quote_sent",
                recipient_email=recipient,
                custom_message=custom_message,
            )
            quote.status = Quote.STATUS_SENT
            quote.save(update_fields=["status"])
            messages.success(request, f"Quote {quote.number} emailed.")
        except Exception as exc:
            messages.error(request, f"Email failed: {exc}")
    elif action == "convert":
        if not quote.user:
            messages.error(request, "Assign a customer to this quote before converting.")
        else:
            invoice = convert_quote_to_invoice(quote, by_user=request.user)
            messages.success(
                request,
                f"Quote {quote.number} converted to invoice {invoice.number}.",
            )
            return redirect("admin_tools:invoice_edit", pk=invoice.pk)
    elif action == "void":
        quote.status = Quote.STATUS_VOID
        quote.save(update_fields=["status"])
        messages.success(request, f"Quote {quote.number} voided.")
    elif action == "clone":
        cloned = create_quote(
            user=quote.user,
            line_items=[
                LineItemSpec(
                    description=item.description,
                    unit_price=item.unit_price,
                    quantity=item.quantity,
                    position=item.position,
                )
                for item in quote.line_items.all()
            ],
            lead_email=quote.lead_email,
            lead_name=quote.lead_name,
            lead_company=quote.lead_company,
            lead_phone=quote.lead_phone,
            vat_rate=quote.vat_rate,
            currency=quote.currency,
            notes=quote.notes,
            internal_notes=f"Cloned from {quote.number}",
            created_by=request.user,
        )
        messages.success(request, f"Quote cloned as {cloned.number}.")
        return redirect("admin_tools:quote_edit", pk=cloned.pk)
    elif action == "delete":
        if quote.status != Quote.STATUS_DRAFT:
            messages.error(request, "Only draft quotes can be deleted.")
        else:
            number = quote.number
            quote.delete()
            messages.success(request, f"Quote {number} deleted.")
            return redirect("admin_tools:quote_list")
    else:
        messages.error(request, f"Unknown action: {action}")

    return redirect("admin_tools:quote_edit", pk=quote.pk)


@staff_member_required
def quote_pdf(request, pk):
    quote = get_object_or_404(Quote, pk=pk)
    pdf_bytes, content_type, ext = render_quote_pdf(
        quote, base_url=request.build_absolute_uri("/")
    )
    disposition = "inline" if request.GET.get("inline") == "1" else "attachment"
    response = HttpResponse(pdf_bytes, content_type=content_type)
    response["Content-Disposition"] = (
        f'{disposition}; filename="quote-{quote.number}.{ext}"'
    )
    return response


@staff_member_required
def cart_builder(request):
    users = User.objects.order_by("email")[:500]
    selected_user = _resolve_builder_user(request)
    cart = None
    if selected_user:
        cart = get_active_cart(selected_user, created_by_staff=request.user)

    context = {
        "users": users,
        "selected_user": selected_user,
        "cart": cart,
        "cart_items": cart.items.all() if cart else [],
        "packages": Package.objects.filter(is_active=True).prefetch_related("features").order_by("card_sort_order", "sort_order", "price_monthly"),
        "featured_tlds": TLDPricing.objects.filter(is_active=True).order_by("registration_price", "tld")[:8],
        "contacts": selected_user.domain_contacts.order_by("-is_default", "label") if selected_user else [],
        "domains": selected_user.domains.order_by("name") if selected_user else [],
    }
    return render(request, "admin_tools/billing/cart_builder.html", context)


@staff_member_required
@require_POST
def cart_builder_add_hosting(request):
    user = _resolve_builder_user(request)
    if not user:
        messages.error(request, "Choose a customer first.")
        return redirect("admin_tools:cart_builder")
    try:
        add_hosting_item(
            user=user,
            package_id=int(request.POST.get("package_id", "0")),
            billing_period=(request.POST.get("billing_period") or "monthly").strip(),
            domain_name=(request.POST.get("domain_name") or "").strip(),
            created_by_staff=request.user,
        )
        messages.success(request, "Hosting plan added to the staff cart.")
    except Exception as exc:
        messages.error(request, str(exc))
    return redirect(f"{reverse('admin_tools:cart_builder')}?user_id={user.pk}")


@staff_member_required
@require_POST
def cart_builder_add_domain(request):
    user = _resolve_builder_user(request)
    if not user:
        messages.error(request, "Choose a customer first.")
        return redirect("admin_tools:cart_builder")
    try:
        add_domain_registration_item(
            user=user,
            domain_name=request.POST.get("domain_name") or "",
            registration_years=int(request.POST.get("registration_years") or 1),
            domain_contact_id=int(request.POST.get("domain_contact_id")) if request.POST.get("domain_contact_id") else None,
            privacy_enabled=request.POST.get("privacy_enabled") == "on",
            auto_renew=request.POST.get("auto_renew") == "on",
            dns_provider=(request.POST.get("dns_provider") or Domain.DNS_PROVIDER_CPANEL).strip(),
            created_by_staff=request.user,
        )
        messages.success(request, "Domain registration added to the staff cart.")
    except Exception as exc:
        messages.error(request, str(exc))
    return redirect(f"{reverse('admin_tools:cart_builder')}?user_id={user.pk}")


@staff_member_required
@require_POST
def cart_builder_add_renewal(request):
    user = _resolve_builder_user(request)
    if not user:
        messages.error(request, "Choose a customer first.")
        return redirect("admin_tools:cart_builder")
    try:
        add_domain_renewal_item(
            user=user,
            domain_id=int(request.POST.get("domain_id") or 0),
            renewal_years=int(request.POST.get("renewal_years") or 1),
            created_by_staff=request.user,
        )
        messages.success(request, "Domain renewal added to the staff cart.")
    except Exception as exc:
        messages.error(request, str(exc))
    return redirect(f"{reverse('admin_tools:cart_builder')}?user_id={user.pk}")


@staff_member_required
@require_POST
def cart_builder_add_transfer(request):
    user = _resolve_builder_user(request)
    if not user:
        messages.error(request, "Choose a customer first.")
        return redirect("admin_tools:cart_builder")
    try:
        add_domain_transfer_item(
            user=user,
            domain_name=request.POST.get("domain_name") or "",
            auth_code=request.POST.get("auth_code") or "",
            domain_contact_id=int(request.POST.get("domain_contact_id")) if request.POST.get("domain_contact_id") else None,
            auto_renew=request.POST.get("auto_renew") == "on",
            dns_provider=(request.POST.get("dns_provider") or Domain.DNS_PROVIDER_CPANEL).strip(),
            created_by_staff=request.user,
        )
        messages.success(request, "Domain transfer added to the staff cart.")
    except Exception as exc:
        messages.error(request, str(exc))
    return redirect(f"{reverse('admin_tools:cart_builder')}?user_id={user.pk}")


@staff_member_required
@require_POST
def cart_builder_remove_item(request, pk):
    user = _resolve_builder_user(request)
    if not user:
        messages.error(request, "Choose a customer first.")
        return redirect("admin_tools:cart_builder")
    remove_cart_item(user=user, item_id=pk, created_by_staff=request.user)
    messages.success(request, "Item removed from the staff cart.")
    return redirect(f"{reverse('admin_tools:cart_builder')}?user_id={user.pk}")


@staff_member_required
@require_POST
def cart_builder_checkout_invoice(request):
    user = _resolve_builder_user(request)
    if not user:
        messages.error(request, "Choose a customer first.")
        return redirect("admin_tools:cart_builder")
    try:
        invoice = create_invoice_from_cart(get_active_cart(user, created_by_staff=request.user), send_email=False)
    except Exception as exc:
        messages.error(request, str(exc))
        return redirect(f"{reverse('admin_tools:cart_builder')}?user_id={user.pk}")
    messages.success(request, f"Invoice {invoice.number} created from the staff cart.")
    return redirect("admin_tools:invoice_edit", pk=invoice.pk)


@staff_member_required
@require_POST
def cart_builder_checkout_quote(request):
    user = _resolve_builder_user(request)
    if not user:
        messages.error(request, "Choose a customer first.")
        return redirect("admin_tools:cart_builder")
    try:
        quote = create_quote_from_cart(get_active_cart(user, created_by_staff=request.user))
    except Exception as exc:
        messages.error(request, str(exc))
        return redirect(f"{reverse('admin_tools:cart_builder')}?user_id={user.pk}")
    messages.success(request, f"Quote {quote.number} created from the staff cart.")
    return redirect("admin_tools:quote_edit", pk=quote.pk)
