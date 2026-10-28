"""Public-facing quote builder + quote acceptance views.

These views are anonymous-accessible (no login required for the builder
itself) and route through ``apps.billing.services`` for persistence.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.decorators.cache import never_cache

from apps.billing.models import BillingDocumentBranding, Quote
from apps.billing.services import (
    create_quote,
    render_quote_pdf,
)
from apps.products.models import Package

logger = logging.getLogger(__name__)


def _client_ip(request) -> str:
    from apps.core.http import get_client_ip

    return get_client_ip(request) or ""


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


@never_cache
def quote_builder(request):
    from apps.core.public_submissions import new_submission_key
    branding = BillingDocumentBranding.get_solo()
    catalogue = _build_catalogue()
    return render(
        request,
        "public/quote_builder.html",
        {
            "branding": branding,
            "catalogue": catalogue,
            "submission_key": new_submission_key(request),
        },
    )


@require_POST
@never_cache
def quote_submit(request):
    """Honeypot-protected public quote submission.

    Expects JSON body:
        {
          "lead": {"name", "email", "company", "phone", "notes"},
          "items": [{"description", "quantity", "unit_price"}, ...],
          "hp": ""  # honeypot - must be empty
        }
    """
    from django import forms
    from apps.core.abuse import bounded_json
    from .public_validation import validate_public_quote
    try:
        if len(request.body) > 64 * 1024:
            return JsonResponse({"error": "The submitted data is too large."}, status=413)
        payload = getattr(request, "public_json", None)
        if payload is None:
            payload = bounded_json(request.body)
        honeypot = payload.get("hp", "")
        if not isinstance(honeypot, str) or len(honeypot) > 200:
            raise ValueError("Invalid form data.")
        if honeypot.strip():
            return JsonResponse({"ok": True, "redirect": reverse("billing_public:quote_thanks")})
        lead, line_items = validate_public_quote(payload)
    except (ValueError, forms.ValidationError) as error:
        message = error.messages[0] if isinstance(error, forms.ValidationError) else str(error)
        return JsonResponse({"error": message}, status=400)
    name, email = lead["name"], lead["email"]

    from apps.core.public_submissions import accept_submission, SubmissionConflict, SubmissionUnavailable
    def create_result():
        quote = create_quote(
            user=request.user if request.user.is_authenticated else None,
            line_items=line_items, lead_email=email, lead_name=name,
            lead_company=lead["company"], lead_phone=lead["phone"], notes=lead["notes"], status=Quote.STATUS_SENT,
        )
        result = {"quote_number": quote.number, "token": str(quote.public_token),
                  "redirect": reverse("billing_public:quote_public", args=[str(quote.public_token)])}
        return result, {"quote_id": quote.pk, "recipient": email}
    try:
        result, created = accept_submission(request, "quote", {"lead": lead, "items": payload["items"]},
                                            create_result, key=payload.get("submission_key"))
    except SubmissionConflict as error:
        return JsonResponse({"error": str(error)}, status=409)
    except SubmissionUnavailable as error:
        response = JsonResponse({"error": str(error)}, status=503)
        response["Retry-After"] = "60"
        return response
    request.session["pending_quote_token"] = result["token"]
    return JsonResponse({"ok": True, "quote_number": result["quote_number"], "redirect": result["redirect"]})


def quote_thanks(request):
    return render(request, "public/quote_thanks.html")


# ---------------------------------------------------------------------------
# Public quote view / accept
# ---------------------------------------------------------------------------


def _get_quote_by_token(token):
    quote = get_object_or_404(Quote, public_token=token)
    return quote


@require_GET
@never_cache
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


@require_GET
@never_cache
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
@never_cache
def quote_public_accept(request, token):
    quote = _get_quote_by_token(token)
    if not request.user.is_authenticated:
        if not quote.is_acceptable:
            messages.error(request, "This quote can no longer be accepted.")
            return redirect("billing_public:quote_public", token=token)
        request.session["pending_quote_token"] = str(quote.public_token)
        login_url = reverse("account_login")
        return redirect(f"{login_url}?next={reverse('billing_public:quote_public_accept_continue', args=[token])}&quote_token={token}")
    return _finalize_quote_acceptance(request, token)


def _finalize_quote_acceptance(request, token):
    from django import forms
    from apps.core.public_submissions import SubmissionUnavailable
    from .acceptance import accept_public_quote
    from apps.portal.cart_service import CartPreflightError
    try:
        invoice = accept_public_quote(request, token)
    except CartPreflightError as exc:
        return render(request, "public/quote_cart_review.html", {
            "quote": _get_quote_by_token(token), "message": str(exc),
            "contact_id": exc.contact_id, "issues": exc.issues,
        }, status=400)
    except forms.ValidationError:
        messages.error(request, "This quote can no longer be accepted.")
        return redirect("billing_public:quote_public", token=token)
    except SubmissionUnavailable:
        response = render(request, "public/quote_acceptance_unavailable.html", {"token": token}, status=503)
        response["Retry-After"] = "60"
        return response
    if request.session.get("pending_quote_token") == str(token):
        request.session.pop("pending_quote_token", None)
    messages.success(request, f"Quote accepted ? invoice {invoice.number} is ready to pay.")
    return redirect("invoices:detail", pk=invoice.pk)


@login_required
@require_http_methods(["GET", "POST"])
@never_cache
def quote_public_accept_continue(request, token):
    """Returning from sign-in only displays confirmation; POST finalizes."""
    if request.method == "POST":
        return _finalize_quote_acceptance(request, token)
    quote = _get_quote_by_token(token)
    from django.core.exceptions import PermissionDenied
    if quote.user_id is not None and quote.user_id != request.user.pk:
        raise PermissionDenied("This quote belongs to another account.")
    return render(request, "public/quote_acceptance_confirm.html", {"quote": quote})
