"""Domain management views."""
from decimal import Decimal
import logging
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.billing.models import Invoice, InvoiceLineItem
from apps.domains.forms import DomainRegistrationForm
from apps.domains.models import Domain, DomainOrder, TLDPricing
from apps.domains.resellerclub_client import ResellerClubClient, ResellerClubError
from apps.domains.services import DomainContactService

logger = logging.getLogger(__name__)

POPULAR_TLDS = ["co.uk", "com", "uk", "org", "net", "io", "org.uk"]

# Basic allow-list: only alphanumeric + hyphens, 2-63 chars
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")

# Cache availability results for 60 seconds to reduce upstream API calls
_CACHE_TTL = 60


def _is_valid_label(label: str) -> bool:
    """Return True if the domain label (part before the TLD) is syntactically valid."""
    return bool(_DOMAIN_LABEL_RE.match(label))


def _rate_limit_key(request) -> str:
    """Build a cache key for per-IP domain check rate limiting."""
    ip = (
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "unknown")
    )
    return f"domain_check_rl:{ip}"


def _split_domain_name(domain_name: str):
    normalized = domain_name.strip().lower()
    for tld in sorted(POPULAR_TLDS, key=len, reverse=True):
        suffix = f".{tld}"
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)], tld
    if "." not in normalized:
        raise ValueError("Please choose a full domain name including the extension.")
    label, tld = normalized.split(".", 1)
    return label, tld


def _build_invoice_number(user_id: int) -> str:
    return f"DOM-{timezone.now():%Y%m%d%H%M%S%f}-{user_id}"


def domain_search(request):
    """Public domain search page."""
    pricing_lookup = TLDPricing.objects.in_bulk(POPULAR_TLDS, field_name="tld")
    featured_tlds = [
        {
            "tld": tld,
            "price": pricing_lookup.get(tld).registration_price if pricing_lookup.get(tld) else None,
        }
        for tld in POPULAR_TLDS
    ]
    return render(request, "domains/search.html", {"popular_tlds": POPULAR_TLDS, "featured_tlds": featured_tlds})


@require_GET
def domain_check(request):
    """
    HTMX endpoint: check domain availability.

    Rate-limited to 20 requests per minute per IP.  Labels are validated
    against an allow-list before being forwarded to the registrar API.
    """
    query = request.GET.get("q", "").strip().lower()
    if not query:
        return HttpResponse("")

    # Rate limiting: 20 checks per minute per IP
    rl_key = _rate_limit_key(request)
    count = cache.get(rl_key, 0)
    if count >= 20:
        return HttpResponse("Too many requests. Please wait a moment.", status=429)
    cache.set(rl_key, count + 1, timeout=60)

    domain_part = query.split(".")[0] if "." in query else query

    if not _is_valid_label(domain_part):
        return HttpResponse("Invalid domain name.", status=400)

    results = []
    client = ResellerClubClient()
    pricing_lookup = TLDPricing.objects.in_bulk(POPULAR_TLDS, field_name="tld")

    for tld in POPULAR_TLDS:
        full_domain = f"{domain_part}.{tld}"
        cache_key = f"domain_avail:{full_domain}"
        cached = cache.get(cache_key)

        if cached is not None:
            available = cached
        else:
            try:
                data = client.check_availability([domain_part], [tld])
                status = data.get(full_domain, {})
                if isinstance(status, dict):
                    available = status.get("status") == "available"
                else:
                    available = str(status).lower() == "available"
                cache.set(cache_key, available, timeout=_CACHE_TTL)
            except ResellerClubError as e:
                logger.warning(f"Domain check failed for {full_domain}: {e}")
                available = None  # Unknown

        results.append({
            "domain": full_domain,
            "tld": tld,
            "available": available,
            "registration_price": pricing_lookup.get(tld).registration_price if pricing_lookup.get(tld) else None,
        })

    return render(request, "domains/partials/availability_results.html", {
        "results": results,
        "query": query,
    })


@login_required
def domain_register(request):
    domain_name = request.GET.get("domain") or request.POST.get("domain_name", "")
    if not domain_name:
        messages.error(request, "Choose a domain before starting checkout.")
        return redirect("domains:search")

    try:
        label, tld = _split_domain_name(domain_name)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("domains:search")

    if not _is_valid_label(label):
        messages.error(request, "Invalid domain name.")
        return redirect("domains:search")

    pricing = TLDPricing.objects.filter(tld=tld, is_active=True).first()
    if not pricing:
        messages.error(request, f"Pricing is not available for .{tld} yet.")
        return redirect("domains:search")

    contact_service = DomainContactService()
    if not request.user.domain_contacts.exists():
        contact_service.ensure_default_contact(request.user)

    if request.method == "POST":
        form = DomainRegistrationForm(request.POST, user=request.user)
        if form.is_valid():
            contact = form.cleaned_data["contact"]
            registration_years = form.cleaned_data["registration_years"]
            total_price = (pricing.registration_price * Decimal(str(registration_years))).quantize(Decimal("0.01"))
            billing_name = request.user.full_name
            billing_address = ""
            if hasattr(request.user, "client_profile"):
                profile = request.user.client_profile
                billing_address = "\n".join(filter(None, [profile.address_line1, profile.address_line2, profile.city, profile.county, profile.postcode, profile.country]))

            invoice = Invoice.objects.create(
                user=request.user,
                number=_build_invoice_number(request.user.id),
                status=Invoice.STATUS_UNPAID,
                vat_rate=Decimal("0.00"),
                due_date=timezone.now().date(),
                billing_name=billing_name,
                billing_address=billing_address,
            )
            InvoiceLineItem.objects.create(
                invoice=invoice,
                description=f"Domain registration: {domain_name.lower()} ({registration_years} year(s))",
                quantity=1,
                unit_price=total_price,
            )
            invoice.calculate_totals()
            order = DomainOrder.objects.create(
                user=request.user,
                invoice=invoice,
                domain_name=domain_name.lower(),
                tld=tld,
                registration_years=registration_years,
                quoted_price=total_price,
                total_price=total_price,
                status=DomainOrder.STATUS_PENDING_PAYMENT,
                privacy_enabled=form.cleaned_data["privacy_enabled"],
                auto_renew=form.cleaned_data["auto_renew"],
                dns_provider=form.cleaned_data["dns_provider"],
                registration_contact=contact,
                admin_contact=contact,
                tech_contact=contact,
                billing_contact=contact,
            )
            messages.success(request, f"Order created for {order.domain_name}. Continue to payment.")
            return redirect("payments:stripe_checkout", invoice_id=invoice.id)
    else:
        form = DomainRegistrationForm(
            user=request.user,
            initial={
                "domain_name": domain_name.lower(),
                "contact": request.user.domain_contacts.filter(is_default=True).first() or request.user.domain_contacts.first(),
            },
        )

    return render(
        request,
        "domains/register.html",
        {
            "form": form,
            "domain_name": domain_name.lower(),
            "unit_price": pricing.registration_price,
        },
    )


@login_required
def my_domains(request):
    """Client portal: list user's domains — paginated."""
    from django.core.paginator import Paginator
    qs = Domain.objects.filter(user=request.user).order_by("name")
    paginator = Paginator(qs, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "domains/my_domains.html", {"domains": page_obj.object_list, "page_obj": page_obj})


@login_required
def domain_detail(request, pk):
    """Domain detail and management page."""
    domain = get_object_or_404(Domain, pk=pk, user=request.user)
    return render(request, "domains/domain_detail.html", {"domain": domain})


@login_required
@require_POST
def domain_toggle_autorenew(request, pk):
    """Toggle auto-renew for a domain."""
    domain = get_object_or_404(Domain, pk=pk, user=request.user)
    domain.auto_renew = not domain.auto_renew
    domain.save(update_fields=["auto_renew"])
    messages.success(request, f"Auto-renew {'enabled' if domain.auto_renew else 'disabled'} for {domain.name}.")
    return redirect("domains:detail", pk=pk)
