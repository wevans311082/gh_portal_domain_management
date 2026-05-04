"""Client portal views."""
from datetime import timedelta
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from apps.services.models import Service
from apps.domains.models import Domain
from apps.billing.models import Invoice, Quote
from apps.support.models import SupportTicket

_PAGE_SIZE = 20


def _build_recent_activity(user):
    """Build a mixed recent activity timeline for the dashboard."""
    items = []

    recent_services = (
        Service.objects.filter(user=user)
        .select_related("package")
        .order_by("-created_at")[:5]
    )
    for service in recent_services:
        items.append(
            {
                "timestamp": service.created_at,
                "title": f"Service created: {service.package.name}",
                "detail": service.domain_name or service.get_status_display(),
                "url": reverse("portal:my_services"),
            }
        )

    recent_domains = (
        Domain.objects.filter(user=user)
        .only("id", "name", "status", "created_at")
        .order_by("-created_at")[:5]
    )
    for domain in recent_domains:
        items.append(
            {
                "timestamp": domain.created_at,
                "title": f"Domain added: {domain.name}",
                "detail": domain.get_status_display(),
                "url": reverse("domains:detail", kwargs={"pk": domain.pk}),
            }
        )

    recent_invoices = (
        Invoice.objects.filter(user=user)
        .only("id", "number", "status", "total", "created_at")
        .order_by("-created_at")[:5]
    )
    for invoice in recent_invoices:
        items.append(
            {
                "timestamp": invoice.created_at,
                "title": f"Invoice issued: {invoice.number}",
                "detail": f"{invoice.get_status_display()} - GBP {invoice.total}",
                "url": reverse("invoices:detail", kwargs={"pk": invoice.pk}),
            }
        )

    paid_invoices = (
        Invoice.objects.filter(user=user, status=Invoice.STATUS_PAID, paid_at__isnull=False)
        .only("id", "number", "paid_at", "total")
        .order_by("-paid_at")[:5]
    )
    for invoice in paid_invoices:
        items.append(
            {
                "timestamp": invoice.paid_at,
                "title": f"Payment received: {invoice.number}",
                "detail": f"GBP {invoice.total}",
                "url": reverse("invoices:detail", kwargs={"pk": invoice.pk}),
            }
        )

    recent_tickets = (
        SupportTicket.objects.filter(user=user)
        .only("id", "subject", "status", "created_at")
        .order_by("-created_at")[:5]
    )
    for ticket in recent_tickets:
        items.append(
            {
                "timestamp": ticket.created_at,
                "title": f"Ticket opened: #{ticket.id}",
                "detail": f"{ticket.subject} ({ticket.get_status_display()})",
                "url": reverse("support:detail", kwargs={"pk": ticket.pk}),
            }
        )

    items.sort(key=lambda x: x["timestamp"] or timezone.now(), reverse=True)
    return items[:12]


@login_required
def dashboard(request):
    """Main client portal dashboard."""
    user = request.user
    now = timezone.now()
    today = timezone.localdate()

    # Use select_related to avoid N+1 queries when templates access FK attributes
    active_services = (
        Service.objects.filter(user=user, status=Service.STATUS_ACTIVE)
        .select_related("package")
    )
    domains = (
        Domain.objects.filter(user=user)
        .only("name", "expires_at", "status", "auto_renew")
        .order_by("expires_at")
    )
    unpaid_invoices = Invoice.objects.filter(
        user=user, status__in=[Invoice.STATUS_UNPAID, Invoice.STATUS_OVERDUE]
    ).only("id", "number", "total", "due_date", "status", "created_at")

    open_tickets = SupportTicket.objects.filter(
        user=user,
        status__in=[SupportTicket.STATUS_OPEN, SupportTicket.STATUS_AWAITING_CLIENT],
    ).only("id", "subject", "status", "priority", "created_at")

    expiring_domains = domains.filter(
        expires_at__lte=now.date() + timedelta(days=30),
        status=Domain.STATUS_ACTIVE,
    )

    expiring_7_days_count = domains.filter(
        expires_at__range=(today, today + timedelta(days=7)),
        status=Domain.STATUS_ACTIVE,
    ).count()
    expiring_30_days_count = expiring_domains.count()

    overdue_invoices_count = unpaid_invoices.filter(status=Invoice.STATUS_OVERDUE).count()
    unpaid_invoices_count = unpaid_invoices.count()

    due_soon_services_count = active_services.filter(
        next_due_date__isnull=False,
        next_due_date__lte=today + timedelta(days=7),
    ).count()

    urgent_tickets_count = open_tickets.filter(
        priority__in=[SupportTicket.PRIORITY_HIGH, SupportTicket.PRIORITY_URGENT]
    ).count()

    paid_last_30_days = Invoice.objects.filter(
        user=user,
        status=Invoice.STATUS_PAID,
        paid_at__gte=now - timedelta(days=30),
    )
    paid_last_30_days_amount = paid_last_30_days.aggregate(total=Sum("total"))["total"] or 0

    recent_activity = _build_recent_activity(user)

    pending_quotes = Quote.objects.filter(
        user=user,
        status__in=[Quote.STATUS_SENT, Quote.STATUS_VIEWED],
    ).order_by("-created_at")[:5]

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
        "expiring_7_days_count": expiring_7_days_count,
        "expiring_30_days_count": expiring_30_days_count,
        "unpaid_invoices_count": unpaid_invoices_count,
        "overdue_invoices_count": overdue_invoices_count,
        "due_soon_services_count": due_soon_services_count,
        "urgent_tickets_count": urgent_tickets_count,
        "paid_last_30_days_amount": paid_last_30_days_amount,
        "auto_renew_enabled_count": domains.filter(
            auto_renew=True,
            status=Domain.STATUS_ACTIVE,
        ).count(),
        "recent_activity": recent_activity,
        "pending_quotes": pending_quotes,
        "pending_quotes_count": pending_quotes.count(),
    }

    return render(request, "portal/dashboard.html", context)


@login_required
def my_services(request):
    """List user's hosting services — paginated."""
    services_qs = (
        Service.objects.filter(user=request.user)
        .select_related("package")
        .order_by("-created_at")
    )
    paginator = Paginator(services_qs, _PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "portal/my_services.html", {"page_obj": page_obj, "services": page_obj.object_list})


@login_required
def my_quotes(request):
    """List the authenticated user's quotes."""
    qs = (
        Quote.objects.filter(user=request.user)
        .order_by("-created_at")
    )
    paginator = Paginator(qs, _PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "portal/my_quotes.html", {"page_obj": page_obj, "quotes": page_obj.object_list})
