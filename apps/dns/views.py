from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from apps.domains.models import Domain
from .models import DNSZone


@login_required
def zone_detail(request, domain_pk):
    domain = get_object_or_404(Domain, pk=domain_pk, user=request.user)
    zone = getattr(domain, "dns_zone", None)
    return render(request, "dns/zone_detail.html", {"domain": domain, "zone": zone})
