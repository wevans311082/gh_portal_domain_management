"""Domain management views."""
import logging
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.views.decorators.http import require_POST
from django.contrib import messages

from apps.domains.models import Domain
from apps.domains.resellerclub_client import ResellerClubClient, ResellerClubError

logger = logging.getLogger(__name__)

POPULAR_TLDS = ["co.uk", "com", "uk", "org", "net", "io", "org.uk"]


def domain_search(request):
    """Public domain search page."""
    return render(request, "domains/search.html", {"popular_tlds": POPULAR_TLDS})


def domain_check(request):
    """HTMX endpoint: check domain availability."""
    query = request.GET.get("q", "").strip().lower()
    if not query:
        return HttpResponse("")

    domain_part = query.split(".")[0] if "." in query else query

    results = []
    client = ResellerClubClient()

    for tld in POPULAR_TLDS:
        full_domain = f"{domain_part}.{tld}"
        try:
            data = client.check_availability([domain_part], [tld])
            status = data.get(full_domain, {})
            if isinstance(status, dict):
                available = status.get("status") == "available"
            else:
                available = str(status).lower() == "available"
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
    """Client portal: list user's domains."""
    domains = Domain.objects.filter(user=request.user).order_by("name")
    return render(request, "domains/my_domains.html", {"domains": domains})


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
