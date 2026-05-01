"""Client portal views."""
from datetime import timedelta
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.utils import timezone

from apps.services.models import Service
from apps.domains.models import Domain
from apps.billing.models import Invoice
from apps.support.models import SupportTicket


@login_required
def dashboard(request):
    """Main client portal dashboard."""
    user = request.user

    active_services = Service.objects.filter(user=user, status=Service.STATUS_ACTIVE)
    domains = Domain.objects.filter(user=user).order_by("expires_at")
    unpaid_invoices = Invoice.objects.filter(
        user=user, status__in=[Invoice.STATUS_UNPAID, Invoice.STATUS_OVERDUE]
    )
    open_tickets = SupportTicket.objects.filter(
        user=user,
        status__in=[SupportTicket.STATUS_OPEN, SupportTicket.STATUS_AWAITING_CLIENT]
    )

    expiring_domains = domains.filter(
        expires_at__lte=timezone.now().date() + timedelta(days=30),
        status=Domain.STATUS_ACTIVE,
    )

    context = {
        "active_services": active_services,
        "domains": domains,
        "unpaid_invoices": unpaid_invoices,
        "open_tickets": open_tickets,
        "expiring_domains": expiring_domains,
        "active_services_count": active_services.count(),
        "domains_count": domains.count(),
        "unpaid_amount": unpaid_invoices.aggregate(total=Sum("total"))["total"] or 0,
        "open_tickets_count": open_tickets.count(),
    }

    return render(request, "portal/dashboard.html", context)


@login_required
def my_services(request):
    """List user's hosting services."""
    services = Service.objects.filter(user=request.user).select_related("package").order_by("-created_at")
    return render(request, "portal/my_services.html", {"services": services})
