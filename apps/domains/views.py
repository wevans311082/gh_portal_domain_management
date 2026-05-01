"""Domain management views."""
import logging
import re
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.contrib import messages
from django.core.cache import cache

from apps.domains.models import Domain
from apps.domains.resellerclub_client import ResellerClubClient, ResellerClubError

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


def domain_search(request):
    """Public domain search page."""
    return render(request, "domains/search.html", {"popular_tlds": POPULAR_TLDS})


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
        })

    return render(request, "domains/partials/availability_results.html", {
        "results": results,
        "query": query,
    })


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
