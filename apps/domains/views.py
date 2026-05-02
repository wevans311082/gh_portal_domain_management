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
from apps.domains.forms import DomainContactForm, DomainRegistrationForm
from apps.domains.models import Domain, DomainContact, DomainOrder, DomainRenewal, TLDPricing
from apps.domains.pricing import TLDPricingService
from apps.domains.resellerclub_client import ResellerClubClient, ResellerClubError
from apps.domains.services import DomainContactService

logger = logging.getLogger(__name__)

POPULAR_TLDS = ["co.uk", "com", "uk", "org", "net", "io", "org.uk"]

# Basic allow-list: only alphanumeric + hyphens, 2-63 chars
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")

# Cache availability results for 60 seconds to reduce upstream API calls
_CACHE_TTL = 60
_PRICING_SYNC_TTL = 900


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


def _sync_missing_tld_pricing(tlds):
    """Sync missing TLD prices from ResellerClub with short cache lock to limit repeat calls."""
    if not tlds:
        return

    lock_key = f"domain_pricing_sync:{','.join(sorted(tlds))}"
    if cache.get(lock_key):
        return

    try:
        TLDPricingService().sync_pricing(tlds=tlds)
        cache.set(lock_key, True, timeout=_PRICING_SYNC_TTL)
    except Exception as exc:
        logger.warning("Unable to sync missing TLD pricing (%s): %s", tlds, exc)


def domain_search(request):
    """Public domain search page."""
    existing_tlds = set(TLDPricing.objects.filter(tld__in=POPULAR_TLDS).values_list("tld", flat=True))
    missing_tlds = [tld for tld in POPULAR_TLDS if tld not in existing_tlds]
    _sync_missing_tld_pricing(missing_tlds)

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

    # If user typed a full domain with TLD (e.g. "example.com"), only check that TLD.
    # Otherwise check all popular TLDs.
    if "." in query:
        _, typed_tld = query.split(".", 1)
        tlds_to_check = [typed_tld]
    else:
        tlds_to_check = POPULAR_TLDS

    existing_tlds = set(TLDPricing.objects.filter(tld__in=tlds_to_check).values_list("tld", flat=True))
    missing_tlds = [tld for tld in tlds_to_check if tld not in existing_tlds]
    _sync_missing_tld_pricing(missing_tlds)

    pricing_lookup = TLDPricing.objects.in_bulk(tlds_to_check, field_name="tld")

    # Split TLDs into those already cached and those needing a live API call
    cache_hits = {}
    tlds_needing_check = []
    for tld in tlds_to_check:
        cached = cache.get(f"domain_avail:{domain_part}.{tld}")
        if cached is not None:
            cache_hits[tld] = cached
        else:
            tlds_needing_check.append(tld)

    # Single bulk API call for all uncached TLDs (ResellerClub supports multi-TLD in one request)
    live_availability = {}
    if tlds_needing_check:
        try:
            client = ResellerClubClient()
            data = client.check_availability([domain_part], tlds_needing_check)
            for tld in tlds_needing_check:
                full_domain = f"{domain_part}.{tld}"
                status = data.get(full_domain, {})
                if isinstance(status, dict):
                    available = status.get("status") == "available"
                else:
                    available = str(status).lower() == "available"
                live_availability[tld] = available
                cache.set(f"domain_avail:{full_domain}", available, timeout=_CACHE_TTL)
        except ResellerClubError as e:
            logger.warning(f"Domain bulk check failed for {domain_part}: {e}")
            for tld in tlds_needing_check:
                live_availability[tld] = None

    results = []
    for tld in tlds_to_check:
        full_domain = f"{domain_part}.{tld}"
        available = cache_hits.get(tld) if tld in cache_hits else live_availability.get(tld)
        pricing = pricing_lookup.get(tld)
        results.append({
            "domain": full_domain,
            "tld": tld,
            "available": available,
            "registration_price": pricing.registration_price if pricing else None,
            "renewal_price": pricing.renewal_price if pricing else None,
            "transfer_price": pricing.transfer_price if pricing else None,
            "whois_url": f"https://lookup.icann.org/en/lookup?name={full_domain}",
        })

    from apps.core.runtime_settings import get_runtime_setting
    api_url = get_runtime_setting("RESELLERCLUB_API_URL", "")
    prices_are_test_mode = "test.httpapi.com" in api_url

    return render(request, "domains/partials/availability_results.html", {
        "results": results,
        "query": query,
        "prices_are_test_mode": prices_are_test_mode,
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
    """Client portal: list user's domains â€" paginated."""
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


@login_required
def domain_renew(request, pk):
    """
    Customer-facing renewal checkout.

    GET  â€" show a confirmation page with the renewal price.
    POST â€" create a DomainRenewal + Invoice, redirect to Stripe checkout.
    """
    domain = get_object_or_404(Domain, pk=pk, user=request.user)

    if domain.status not in (Domain.STATUS_ACTIVE, Domain.STATUS_EXPIRED):
        messages.error(request, "This domain cannot be renewed at this time.")
        return redirect("domains:detail", pk=pk)

    pricing = TLDPricing.objects.filter(tld=domain.tld, is_active=True).first()
    if not pricing:
        messages.error(request, f"Renewal pricing is not available for .{domain.tld}.")
        return redirect("domains:detail", pk=pk)

    years = max(1, min(int(request.POST.get("years", 1)), 10))
    renewal_price = (pricing.renewal_price * Decimal(str(years))).quantize(Decimal("0.01"))

    if request.method == "POST":
        billing_name = request.user.full_name
        billing_address = ""
        if hasattr(request.user, "client_profile"):
            profile = request.user.client_profile
            billing_address = "\n".join(filter(None, [
                profile.address_line1, profile.address_line2,
                profile.city, profile.county, profile.postcode, profile.country,
            ]))

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
            description=f"Domain renewal: {domain.name} ({years} year(s))",
            quantity=1,
            unit_price=renewal_price,
        )
        invoice.calculate_totals()

        renewal = DomainRenewal.objects.create(
            domain=domain,
            user=request.user,
            invoice=invoice,
            renewal_years=years,
            total_price=renewal_price,
            status=DomainRenewal.STATUS_PENDING_PAYMENT,
        )

        messages.success(request, f"Renewal order created for {domain.name}. Continue to payment.")
        return redirect("payments:stripe_checkout", invoice_id=invoice.id)

    return render(request, "domains/renew.html", {
        "domain": domain,
        "pricing": pricing,
        "years": years,
        "renewal_price": renewal_price,
    })


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Domain Contact portal UI
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

@login_required
def contact_list(request):
    """List the current user's domain contacts."""
    contacts = DomainContact.objects.filter(user=request.user).order_by("-is_default", "label")
    return render(request, "domains/contacts/list.html", {"contacts": contacts})


@login_required
def contact_create(request):
    """Create a new domain contact."""
    if request.method == "POST":
        form = DomainContactForm(request.POST)
        if form.is_valid():
            contact = form.save(commit=False)
            contact.user = request.user
            if contact.is_default:
                # Demote any existing default first
                DomainContact.objects.filter(user=request.user, is_default=True).update(is_default=False)
            contact.save()
            messages.success(request, f'Contact "{contact.label}" created.')
            return redirect("domains:contact_list")
    else:
        # Pre-fill from profile if no contacts yet
        service = DomainContactService()
        initial = {}
        if not request.user.domain_contacts.exists():
            initial = service.build_default_contact(request.user)
            initial["is_default"] = True
        form = DomainContactForm(initial=initial)

    return render(request, "domains/contacts/form.html", {"form": form, "action": "Create"})


@login_required
def contact_edit(request, pk):
    """Edit an existing domain contact."""
    contact = get_object_or_404(DomainContact, pk=pk, user=request.user)
    was_default = contact.is_default  # capture before form._post_clean mutates instance

    if request.method == "POST":
        form = DomainContactForm(request.POST, instance=contact)
        if form.is_valid():
            if form.cleaned_data["is_default"] and not was_default:
                DomainContact.objects.filter(user=request.user, is_default=True).update(is_default=False)
            form.save()
            messages.success(request, f'Contact "{contact.label}" updated.')
            return redirect("domains:contact_list")
    else:
        form = DomainContactForm(instance=contact)

    return render(request, "domains/contacts/form.html", {
        "form": form,
        "contact": contact,
        "action": "Edit",
    })


@login_required
@require_POST
def contact_delete(request, pk):
    """Delete a domain contact (blocked if it is in use by an active domain order)."""
    contact = get_object_or_404(DomainContact, pk=pk, user=request.user)

    # Prevent deletion if contact is attached to any order
    in_use = (
        contact.registration_orders.exists()
        or contact.admin_orders.exists()
        or contact.tech_orders.exists()
        or contact.billing_orders.exists()
    )
    if in_use:
        messages.error(request, f'"{contact.label}" is attached to a domain order and cannot be deleted.')
        return redirect("domains:contact_list")

    label = contact.label
    contact.delete()
    messages.success(request, f'Contact "{label}" deleted.')
    return redirect("domains:contact_list")


@login_required
@require_POST
def contact_set_default(request, pk):
    """Make a contact the default without editing it."""
    contact = get_object_or_404(DomainContact, pk=pk, user=request.user)
    DomainContact.objects.filter(user=request.user, is_default=True).update(is_default=False)
    contact.is_default = True
    contact.save(update_fields=["is_default"])
    messages.success(request, f'"{contact.label}" is now your default contact.')
    return redirect("domains:contact_list")

