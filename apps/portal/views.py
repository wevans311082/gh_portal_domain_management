"""Client portal views."""
import csv
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.domains.models import Domain, TLDPricing
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
from apps.products.models import Package
from apps.services.models import Service
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


@login_required
def shop(request):
    cart = get_active_cart(request.user)
    packages = (
        Package.objects.filter(is_active=True)
        .prefetch_related("features")
        .order_by("card_sort_order", "sort_order", "price_monthly")
    )
    featured_tlds = TLDPricing.objects.filter(is_active=True).order_by("registration_cost", "tld")[:8]
    contacts = request.user.domain_contacts.order_by("-is_default", "label")
    renewable_domains = request.user.domains.order_by("name")
    return render(
        request,
        "portal/shop.html",
        {
            "cart": cart,
            "cart_items": cart.items.all(),
            "cart_count": cart.items.count(),
            "packages": packages,
            "featured_tlds": featured_tlds,
            "contacts": contacts,
            "renewable_domains": renewable_domains,
            "prefill_domain": (request.GET.get("domain") or "").strip().lower(),
        },
    )


@login_required
def cart_detail(request):
    cart = get_active_cart(request.user)
    return render(
        request,
        "portal/cart.html",
        {
            "cart": cart,
            "cart_items": cart.items.all(),
            "cart_count": cart.items.count(),
        },
    )


@login_required
@require_POST
def cart_add_hosting(request):
    try:
        add_hosting_item(
            user=request.user,
            package_id=int(request.POST.get("package_id", "0")),
            billing_period=(request.POST.get("billing_period") or "monthly").strip(),
            domain_name=(request.POST.get("domain_name") or "").strip(),
        )
    except Exception as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Hosting plan added to your cart.")
    return redirect(request.POST.get("next") or reverse("portal:shop"))


@login_required
@require_POST
def cart_add_domain(request):
    try:
        add_domain_registration_item(
            user=request.user,
            domain_name=request.POST.get("domain_name") or "",
            registration_years=int(request.POST.get("registration_years") or 1),
            domain_contact_id=int(request.POST.get("domain_contact_id")) if request.POST.get("domain_contact_id") else None,
            privacy_enabled=request.POST.get("privacy_enabled") == "on",
            auto_renew=request.POST.get("auto_renew") == "on",
            dns_provider=(request.POST.get("dns_provider") or Domain.DNS_PROVIDER_CPANEL).strip(),
        )
    except Exception as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Domain added to your cart.")
    return redirect(request.POST.get("next") or reverse("portal:shop"))


@login_required
@require_POST
def cart_add_renewal(request):
    try:
        add_domain_renewal_item(
            user=request.user,
            domain_id=int(request.POST.get("domain_id") or 0),
            renewal_years=int(request.POST.get("renewal_years") or 1),
        )
    except Exception as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Domain renewal added to your cart.")
    return redirect(request.POST.get("next") or reverse("portal:shop"))


@login_required
@require_POST
def cart_add_transfer(request):
    try:
        add_domain_transfer_item(
            user=request.user,
            domain_name=request.POST.get("domain_name") or "",
            auth_code=request.POST.get("auth_code") or "",
            domain_contact_id=int(request.POST.get("domain_contact_id")) if request.POST.get("domain_contact_id") else None,
            auto_renew=request.POST.get("auto_renew") == "on",
            dns_provider=(request.POST.get("dns_provider") or Domain.DNS_PROVIDER_CPANEL).strip(),
        )
    except Exception as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Domain transfer added to your cart.")
    return redirect(request.POST.get("next") or reverse("portal:shop"))


@login_required
@require_POST
def cart_remove_item(request, pk):
    remove_cart_item(user=request.user, item_id=pk)
    messages.success(request, "Item removed from your cart.")
    return redirect(request.POST.get("next") or reverse("portal:cart"))


@login_required
@require_POST
def cart_checkout_invoice(request):
    try:
        invoice = create_invoice_from_cart(get_active_cart(request.user))
    except Exception as exc:
        messages.error(request, str(exc))
        return redirect(reverse("portal:cart"))
    messages.success(request, f"Invoice {invoice.number} created from your cart.")
    return redirect("invoices:detail", pk=invoice.pk)


@login_required
@require_POST
def cart_checkout_quote(request):
    try:
        quote = create_quote_from_cart(get_active_cart(request.user))
    except Exception as exc:
        messages.error(request, str(exc))
        return redirect(reverse("portal:cart"))
    messages.success(request, f"Quote {quote.number} created from your cart.")
    return redirect("billing_public:quote_public", token=quote.public_token)


# ---------------------------------------------------------------------------
# Phase 3: Account statement + CSV export
# ---------------------------------------------------------------------------

@login_required
def account_statement(request):
    """Display all invoices for the current user within an optional date range."""
    from apps.payments.models import Payment

    date_from_str = request.GET.get("date_from", "")
    date_to_str = request.GET.get("date_to", "")

    invoices = Invoice.objects.filter(user=request.user).order_by("-issue_date")

    try:
        from datetime import date
        if date_from_str:
            invoices = invoices.filter(issue_date__gte=date.fromisoformat(date_from_str))
        if date_to_str:
            invoices = invoices.filter(issue_date__lte=date.fromisoformat(date_to_str))
    except ValueError:
        messages.error(request, "Invalid date format. Use YYYY-MM-DD.")

    totals = invoices.aggregate(
        total_invoiced=Sum("total"),
        total_paid=Sum("amount_paid"),
    )

    if request.GET.get("export") == "csv":
        return _statement_csv_response(invoices)

    paginator = Paginator(invoices, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "portal/account_statement.html", {
        "page_obj": page_obj,
        "date_from": date_from_str,
        "date_to": date_to_str,
        "totals": totals,
    })


def _statement_csv_response(invoices):
    """Return a streaming CSV download of invoice records."""
    def _rows():
        yield ["Invoice #", "Date", "Due Date", "Status", "Total", "Paid", "Outstanding"]
        for inv in invoices.iterator():
            yield [
                inv.number,
                inv.issue_date,
                inv.due_date or "",
                inv.status,
                inv.total,
                inv.amount_paid,
                inv.amount_outstanding,
            ]

    class _Echo:
        def write(self, value):
            return value

    writer = csv.writer(_Echo())
    response = StreamingHttpResponse(
        (writer.writerow(row) for row in _rows()),
        content_type="text/csv",
    )
    response["Content-Disposition"] = 'attachment; filename="account-statement.csv"'
    return response


@login_required
def notification_preferences(request):
    from apps.notifications.models import NotificationPreference
    from apps.notifications.services import NOTIFICATION_TEMPLATES

    user = request.user
    all_template_names = sorted(NOTIFICATION_TEMPLATES.keys())

    if request.method == "POST":
        for name in all_template_names:
            enabled = request.POST.get(f"pref_{name}") == "1"
            NotificationPreference.objects.update_or_create(
                user=user,
                template_name=name,
                defaults={"enabled": enabled},
            )
        messages.success(request, "Notification preferences saved.")
        return redirect("portal:notification_preferences")

    prefs = {p.template_name: p.enabled for p in NotificationPreference.objects.filter(user=user)}
    notifications = [
        {
            "name": name,
            "label": name.replace("_", " ").title(),
            "enabled": prefs.get(name, True),
        }
        for name in all_template_names
    ]
    return render(request, "portal/notification_preferences.html", {"notifications": notifications})


# ---------------------------------------------------------------------------
# Phase 6 — Hosting self-service
# ---------------------------------------------------------------------------

@login_required
def hosting_sso(request, service_pk):
    """Generate a cPanel SSO URL and redirect the user to cPanel."""
    from apps.services.models import Service
    from apps.provisioning.whm_client import WHMClient
    service = get_object_or_404(Service, pk=service_pk, user=request.user)
    if not service.cpanel_username:
        messages.error(request, "No cPanel username configured for this service.")
        return redirect("portal:my_services")
    try:
        client = WHMClient()
        sso_url = client.create_cpanel_session(service.cpanel_username)
        return redirect(sso_url)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("cPanel SSO failed for service %s: %s", service_pk, e)
        messages.error(request, "Could not log you in to cPanel. Please try again later.")
        return redirect("portal:my_services")


@login_required
def hosting_usage(request, service_pk):
    """Show disk & bandwidth usage for a hosting service."""
    from apps.services.models import Service
    from apps.provisioning.whm_client import WHMClient
    service = get_object_or_404(Service, pk=service_pk, user=request.user)
    quota = {}
    if service.cpanel_username:
        try:
            client = WHMClient()
            quota = client.get_quota(service.cpanel_username)
        except Exception:
            pass
    return render(request, "portal/hosting_usage.html", {"service": service, "quota": quota})


@login_required
@require_POST
def apply_promo_code(request):
    """Apply a promo code to the user's active cart."""
    from apps.core.models import PromoCode
    code_str = (request.POST.get("code") or "").strip().upper()
    cart = get_active_cart(request.user)
    if not code_str:
        messages.error(request, "Please enter a promo code.")
        return redirect("portal:cart")
    try:
        promo = PromoCode.objects.get(code__iexact=code_str)
        if promo.is_valid():
            cart.promo_code = promo
            cart.save(update_fields=["promo_code", "updated_at"])
            messages.success(request, f"Promo code '{code_str}' applied.")
        else:
            messages.error(request, f"Promo code '{code_str}' is not valid or has expired.")
    except PromoCode.DoesNotExist:
        messages.error(request, f"Promo code '{code_str}' not found.")
    return redirect("portal:cart")


@login_required
def login_history(request):
    """Show the user's last 50 login events from the audit log."""
    from apps.audit.models import AuditLog
    logs = AuditLog.objects.filter(user=request.user, action__startswith="login").order_by("-created_at")[:50]
    return render(request, "portal/login_history.html", {"logs": logs})


@login_required
def gdpr_data_export(request):
    """Return a JSON file containing all personal data for the current user."""
    import json
    from django.http import HttpResponse
    from apps.billing.models import Invoice
    from apps.payments.models import Payment
    from apps.domains.models import Domain
    from apps.services.models import Service

    data = {
        "user": {
            "email": request.user.email,
            "first_name": request.user.first_name,
            "last_name": request.user.last_name,
            "date_joined": request.user.date_joined.isoformat(),
        },
        "invoices": list(
            Invoice.objects.filter(user=request.user).values(
                "id", "status", "total", "issued_at", "due_at"
            )
        ),
        "payments": list(
            Payment.objects.filter(user=request.user).values(
                "id", "amount", "status", "created_at"
            )
        ),
        "domains": list(
            Domain.objects.filter(user=request.user).values(
                "domain_name", "status", "expiry_date"
            )
        ),
        "services": list(
            Service.objects.filter(user=request.user).values(
                "id", "name", "status", "created_at"
            )
        ),
    }
    payload = json.dumps(data, indent=2, default=str)
    response = HttpResponse(payload, content_type="application/json")
    response["Content-Disposition"] = 'attachment; filename="my_data.json"'
    return response


@login_required
def api_key_list(request):
    from apps.core.models import APIKey
    keys = APIKey.objects.filter(user=request.user, is_active=True).order_by("-created_at")
    return render(request, "portal/api_keys.html", {"keys": keys})


@login_required
def api_key_create(request):
    from apps.core.models import APIKey
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Please provide a name for the API key.")
            return redirect("portal:api_key_create")
        key_obj, raw_key = APIKey.generate(user=request.user, name=name)
        # Show raw key once — pass it in context on redirect via session
        request.session["new_api_key"] = raw_key
        request.session["new_api_key_id"] = key_obj.pk
        return redirect("portal:api_key_list")
    return render(request, "portal/api_key_create.html")


@login_required
@require_POST
def api_key_revoke(request, pk):
    from apps.core.models import APIKey
    key = get_object_or_404(APIKey, pk=pk, user=request.user)
    key.is_active = False
    key.save(update_fields=["is_active"])
    messages.success(request, f"API key '{key.name}' revoked.")
    return redirect("portal:api_key_list")
