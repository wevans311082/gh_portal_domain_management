"""Public-facing quote builder + quote acceptance views.

These views are anonymous-accessible (no login required for the builder
itself) and route through ``apps.billing.services`` for persistence.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import List

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.billing.models import BillingDocumentBranding, Quote
from apps.billing.services import (
    LineItemSpec,
    convert_quote_to_invoice,
    create_quote,
    email_document,
    render_quote_pdf,
)
from apps.products.models import Package

logger = logging.getLogger(__name__)


def _client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


def _build_catalogue():
    packages = Package.objects.filter(is_active=True, is_quotable=True).order_by(
        "quote_category", "sort_order", "price_monthly"
    )
    grouped: dict[str, list] = {}
    for pkg in packages:
        cat = pkg.quote_category or "Services"
        grouped.setdefault(cat, []).append(
            {
                "id": pkg.pk,
                "slug": pkg.slug,
                "name": pkg.name,
                "blurb": pkg.quote_blurb or pkg.description,
                "price_monthly": float(pkg.price_monthly),
                "price_annually": float(pkg.price_annually),
                "setup_fee": float(pkg.setup_fee),
            }
        )
    return [{"category": k, "items": v} for k, v in grouped.items()]


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def quote_builder(request):
    branding = BillingDocumentBranding.get_solo()
    catalogue = _build_catalogue()
    return render(
        request,
        "public/quote_builder.html",
        {
            "branding": branding,
            "catalogue": catalogue,
            "catalogue_json": json.dumps(catalogue),
        },
    )


@require_POST
def quote_submit(request):
    """Honeypot-protected public quote submission.

    Expects JSON body:
        {
          "lead": {"name", "email", "company", "phone", "notes"},
          "items": [{"description", "quantity", "unit_price"}, ...],
          "hp": ""  # honeypot - must be empty
        }
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if (payload.get("hp") or "").strip():
        # silent reject
        logger.warning("Quote submit honeypot tripped from %s", _client_ip(request))
        return JsonResponse({"ok": True, "redirect": reverse("billing_public:quote_thanks")})

    lead = payload.get("lead") or {}
    email = (lead.get("email") or "").strip()
    name = (lead.get("name") or "").strip()
    if not email or not name:
        return JsonResponse({"error": "Name and email are required."}, status=400)

    raw_items = payload.get("items") or []
    line_items: List[LineItemSpec] = []
    for idx, item in enumerate(raw_items):
        desc = (item.get("description") or "").strip()
        if not desc:
            continue
        try:
            qty = Decimal(str(item.get("quantity") or "1"))
            price = Decimal(str(item.get("unit_price") or "0"))
        except (InvalidOperation, ValueError):
            continue
        line_items.append(
            LineItemSpec(description=desc, quantity=qty, unit_price=price, position=idx)
        )

    if not line_items:
        return JsonResponse({"error": "Add at least one item to your quote."}, status=400)

    user = request.user if request.user.is_authenticated else None

    quote = create_quote(
        user=user,
        line_items=line_items,
        lead_email=email,
        lead_name=name,
        lead_company=(lead.get("company") or "").strip(),
        lead_phone=(lead.get("phone") or "").strip(),
        notes=(lead.get("notes") or "").strip(),
        status=Quote.STATUS_SENT,
    )

    # Save token in session so we can link Quote.user post-registration.
    request.session["pending_quote_token"] = str(quote.public_token)

    try:
        email_document(quote, kind="quote_sent")
    except Exception:
        logger.exception("Failed to email quote %s", quote.number)

    return JsonResponse(
        {
            "ok": True,
            "quote_number": quote.number,
            "redirect": reverse("billing_public:quote_public", args=[str(quote.public_token)]),
        }
    )


def quote_thanks(request):
    return render(request, "public/quote_thanks.html")


# ---------------------------------------------------------------------------
# Public quote view / accept
# ---------------------------------------------------------------------------


def _get_quote_by_token(token):
    quote = get_object_or_404(Quote, public_token=token)
    return quote


def quote_public(request, token):
    quote = _get_quote_by_token(token)
    branding = BillingDocumentBranding.get_solo()

    # First view marks it as viewed
    if quote.status == Quote.STATUS_SENT:
        quote.status = Quote.STATUS_VIEWED
        quote.save(update_fields=["status"])

    return render(
        request,
        "public/quote_public.html",
        {"quote": quote, "branding": branding},
    )


def quote_public_pdf(request, token):
    quote = _get_quote_by_token(token)
    pdf_bytes, content_type, ext = render_quote_pdf(
        quote, base_url=request.build_absolute_uri("/")
    )
    disposition = "inline" if request.GET.get("inline") == "1" else "attachment"
    response = HttpResponse(pdf_bytes, content_type=content_type)
    response["Content-Disposition"] = (
        f'{disposition}; filename="quote-{quote.number}.{ext}"'
    )
    return response


@require_POST
def quote_public_accept(request, token):
    quote = _get_quote_by_token(token)

    if not quote.is_acceptable:
        messages.error(request, "This quote can no longer be accepted.")
        return redirect("billing_public:quote_public", token=token)

    if not request.user.is_authenticated:
        # Park the token; bounce to register/login with quote_token in URL
        request.session["pending_quote_token"] = str(quote.public_token)
        login_url = reverse("account_login")
        return redirect(f"{login_url}?next={reverse('billing_public:quote_public_accept_continue', args=[token])}&quote_token={token}")

    # Link quote to logged-in user if not already linked
    if quote.user_id is None:
        quote.user = request.user
        quote.save(update_fields=["user"])

    quote.accepted_at = timezone.now()
    quote.accepted_by_ip = _client_ip(request)
    quote.save(update_fields=["accepted_at", "accepted_by_ip"])

    invoice = convert_quote_to_invoice(quote, by_user=request.user)
    messages.success(
        request,
        f"Quote {quote.number} accepted — invoice {invoice.number} is ready to pay.",
    )
    return redirect("invoices:detail", pk=invoice.pk)


@login_required
def quote_public_accept_continue(request, token):
    """After login/register, finalize an acceptance that was started anonymously."""
    quote = _get_quote_by_token(token)
    if not quote.is_acceptable:
        messages.error(request, "This quote can no longer be accepted.")
        return redirect("billing_public:quote_public", token=token)

    if quote.user_id is None:
        quote.user = request.user
        quote.save(update_fields=["user"])

    quote.accepted_at = timezone.now()
    quote.accepted_by_ip = _client_ip(request)
    quote.save(update_fields=["accepted_at", "accepted_by_ip"])

    invoice = convert_quote_to_invoice(quote, by_user=request.user)
    request.session.pop("pending_quote_token", None)
    messages.success(
        request,
        f"Quote {quote.number} accepted — invoice {invoice.number} is ready to pay.",
    )
    return redirect("invoices:detail", pk=invoice.pk)
