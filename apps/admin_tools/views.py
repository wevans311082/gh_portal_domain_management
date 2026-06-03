import json
import time
from decimal import Decimal, InvalidOperation
from datetime import date, timedelta
import requests

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask
from django_celery_results.models import TaskResult

from apps.core.runtime_settings import get_runtime_setting
from apps.accounts.models import User
from apps.audit.models import AuditLog, EmailLog
from apps.billing.models import Invoice
from apps.domains.models import Domain, DomainPricingSettings, DomainRenewal, TLDPricing
from apps.domains.resellerclub_client import ResellerClubClient
from apps.payments.models import Payment
from apps.products.models import Package
from apps.provisioning.whm_client import WHMClient, generate_cpanel_username, generate_secure_password
from apps.provisioning.whm_sync import WHMSyncService
from apps.services.models import Service
from apps.support.models import SupportTicket
from . import wizard_views
from .forms import Microsoft365GraphSettingsForm
from .decorators import staff_member_required


def _build_task_summary():
    recent_results = TaskResult.objects.order_by("-date_done")[:20]
    last_week = timezone.now() - timedelta(days=7)
    recent_week_results = TaskResult.objects.filter(date_done__gte=last_week)
    status_counts = {
        "success": recent_week_results.filter(status="SUCCESS").count(),
        "failure": recent_week_results.filter(status="FAILURE").count(),
        "started": recent_week_results.filter(status="STARTED").count(),
    }
    max_count = max(status_counts.values(), default=0) or 1
    chart_bars = [
        {
            "label": "Successful",
            "count": status_counts["success"],
            "height": max(12, round((status_counts["success"] / max_count) * 100)),
            "color": "bg-emerald-500",
        },
        {
            "label": "Failed",
            "count": status_counts["failure"],
            "height": max(12, round((status_counts["failure"] / max_count) * 100)),
            "color": "bg-rose-500",
        },
        {
            "label": "Started",
            "count": status_counts["started"],
            "height": max(12, round((status_counts["started"] / max_count) * 100)),
            "color": "bg-amber-500",
        },
    ]

    return {
        "recent_results": recent_results,
        "task_status_counts": status_counts,
        "task_chart_bars": chart_bars,
        "enabled_periodic_tasks": PeriodicTask.objects.filter(enabled=True).count(),
        "disabled_periodic_tasks": PeriodicTask.objects.filter(enabled=False).count(),
        "interval_schedules": IntervalSchedule.objects.count(),
        "crontab_schedules": CrontabSchedule.objects.count(),
        "failed_task_results": TaskResult.objects.filter(status="FAILURE").count(),
        "periodic_task_admin_url": reverse("admin:django_celery_beat_periodictask_changelist"),
        "interval_schedule_admin_url": reverse("admin:django_celery_beat_intervalschedule_changelist"),
        "crontab_schedule_admin_url": reverse("admin:django_celery_beat_crontabschedule_changelist"),
        "task_result_admin_url": reverse("admin:django_celery_results_taskresult_changelist"),
    }


@staff_member_required
def dashboard(request):
    now = timezone.now()
    last_30 = now - timedelta(days=30)
    last_7 = now - timedelta(days=7)

    recent_users = User.objects.order_by("-created_at")[:5]
    recent_tickets = SupportTicket.objects.select_related("user").order_by("-created_at")[:5]
    recent_audit = AuditLog.objects.select_related("user").order_by("-created_at")[:8]
    open_tickets = SupportTicket.objects.filter(
        status__in=["open", "awaiting_support", "awaiting_client", "on_hold"]
    ).count()
    revenue_30d = Invoice.objects.filter(
        status=Invoice.STATUS_PAID, paid_at__gte=last_30
    ).aggregate(total=Sum("total"))["total"] or 0
    new_users_30d = User.objects.filter(created_at__gte=last_30).count()
    failed_tasks_7d = TaskResult.objects.filter(status="FAILURE", date_done__gte=last_7).count()

    context = {
        "total_users": User.objects.count(),
        "staff_without_mfa": User.objects.filter(is_staff=True, mfa_enabled=False).count(),
        "active_services": Service.objects.filter(status="active").count(),
        "unpaid_invoices": Invoice.objects.filter(status=Invoice.STATUS_UNPAID).count(),
        "total_domains": Domain.objects.count(),
        "open_tickets": open_tickets,
        "revenue_30d": revenue_30d,
        "new_users_30d": new_users_30d,
        "failed_tasks_7d": failed_tasks_7d,
        "recent_users": recent_users,
        "recent_tickets": recent_tickets,
        "recent_audit": recent_audit,
    }

    # Quote pipeline widget
    from apps.billing.models import Quote
    open_quote_statuses = [Quote.STATUS_SENT, Quote.STATUS_VIEWED]
    context["quotes_open"] = Quote.objects.filter(status__in=open_quote_statuses).count()
    context["quotes_pipeline_value"] = (
        Quote.objects.filter(status__in=open_quote_statuses).aggregate(t=Sum("total"))["t"] or 0
    )
    context["quotes_accepted_30d"] = Quote.objects.filter(
        status__in=[Quote.STATUS_ACCEPTED, Quote.STATUS_CONVERTED],
        updated_at__gte=last_30,
    ).count()
    sent_30d = Quote.objects.filter(created_at__gte=last_30).exclude(status=Quote.STATUS_DRAFT).count()
    context["quotes_conversion_30d"] = (
        round(100.0 * context["quotes_accepted_30d"] / sent_30d, 1) if sent_30d else 0.0
    )
    context.update(_build_task_summary())
    return render(request, "admin_tools/dashboard.html", context)


@staff_member_required
def task_management(request):
    context = _build_task_summary()
    context["periodic_tasks"] = PeriodicTask.objects.select_related("interval", "crontab").order_by(
        "name"
    )
    return render(request, "admin_tools/task_management.html", context)


@staff_member_required
def stats(request):
    """Admin statistics dashboard: revenue, domain counts, expiring domains, task health."""
    today = timezone.now().date()
    twelve_months_ago = timezone.now() - timedelta(days=365)

    # ── Revenue by month (last 12 months, paid invoices) ─────────────────────
    monthly_revenue = (
        Invoice.objects
        .filter(status=Invoice.STATUS_PAID, paid_at__gte=twelve_months_ago)
        .annotate(month=TruncMonth("paid_at"))
        .values("month")
        .annotate(revenue=Sum("total"))
        .order_by("month")
    )
    revenue_labels = [r["month"].strftime("%b %Y") for r in monthly_revenue]
    revenue_values = [float(r["revenue"] or 0) for r in monthly_revenue]
    total_revenue_12m = sum(revenue_values)

    # ── Domain counts by status ───────────────────────────────────────────────
    domain_status_counts = (
        Domain.objects
        .values("status")
        .annotate(count=Count("id"))
        .order_by("status")
    )
    total_domains = Domain.objects.count()
    active_domains = Domain.objects.filter(status=Domain.STATUS_ACTIVE).count()

    # ── Domains expiring in next 30 days ──────────────────────────────────────
    expiring_soon = Domain.objects.select_related("user").filter(
        status=Domain.STATUS_ACTIVE,
        expires_at__range=(today, today + timedelta(days=30)),
    ).order_by("expires_at")

    # ── New signups by month ──────────────────────────────────────────────────
    monthly_signups = (
        User.objects
        .filter(created_at__gte=twelve_months_ago)
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(count=Count("id"))
        .order_by("month")
    )
    signup_labels = [r["month"].strftime("%b %Y") for r in monthly_signups]
    signup_values = [r["count"] for r in monthly_signups]
    total_new_users_12m = sum(signup_values)

    # ── Renewal stats ─────────────────────────────────────────────────────────
    renewal_counts = {
        "completed": DomainRenewal.objects.filter(status=DomainRenewal.STATUS_COMPLETED).count(),
        "failed": DomainRenewal.objects.filter(status=DomainRenewal.STATUS_FAILED).count(),
        "pending": DomainRenewal.objects.filter(
            status__in=[DomainRenewal.STATUS_PENDING_PAYMENT, DomainRenewal.STATUS_PAID, DomainRenewal.STATUS_PROCESSING]
        ).count(),
    }

    # ── Task health (last 7 days) ─────────────────────────────────────────────
    task_summary = _build_task_summary()

    context = {
        # Revenue
        "revenue_labels": revenue_labels,
        "revenue_values": revenue_values,
        "total_revenue_12m": total_revenue_12m,
        # Domains
        "domain_status_counts": list(domain_status_counts),
        "total_domains": total_domains,
        "active_domains": active_domains,
        "expiring_soon": expiring_soon,
        # Signups
        "signup_labels": signup_labels,
        "signup_values": signup_values,
        "total_new_users_12m": total_new_users_12m,
        "total_users": User.objects.count(),
        # Renewals
        "renewal_counts": renewal_counts,
        # Services
        "active_services": Service.objects.filter(status="active").count(),
        "unpaid_invoices": Invoice.objects.filter(status=Invoice.STATUS_UNPAID).count(),
    }
    context.update(task_summary)
    return render(request, "admin_tools/stats.html", context)


# ---------------------------------------------------------------------------
# Website template scan
# ---------------------------------------------------------------------------

@staff_member_required
def template_scan(request):
    """Trigger import of website templates from the ZIP archive folder."""
    from apps.website_templates.tasks import import_templates
    from apps.website_templates.models import WebsiteTemplate

    result = None
    error = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "scan":
            try:
                task = import_templates.delay(force=False)
                messages.success(request, f"Template scan started (task {task.id}). Refresh in a moment.")
                return redirect(reverse("admin_tools:template_scan"))
            except Exception as exc:
                error = str(exc)
                # Fallback: run synchronously
                try:
                    from django.core.management import call_command
                    import io
                    out = io.StringIO()
                    call_command("import_website_templates", stdout=out, stderr=out)
                    result = out.getvalue()
                    messages.success(request, "Template scan completed (synchronous fallback).")
                except Exception as sync_exc:
                    error = str(sync_exc)

        elif action == "force":
            try:
                from django.core.management import call_command
                import io
                out = io.StringIO()
                call_command("import_website_templates", force=True, stdout=out, stderr=out)
                result = out.getvalue()
                messages.success(request, "Force re-import completed.")
            except Exception as exc:
                error = str(exc)

    template_count = WebsiteTemplate.objects.count()
    active_count = WebsiteTemplate.objects.filter(is_active=True).count()
    return render(request, "admin_tools/template_scan.html", {
        "template_count": template_count,
        "active_count": active_count,
        "result": result,
        "error": error,
    })


# ---------------------------------------------------------------------------
# Integration diagnostic helpers
# ---------------------------------------------------------------------------

def _probe(label, fn):
    """
    Run *fn()* and return a dict capturing the outcome:
      status, elapsed_ms, response_data, error
    """
    start = time.monotonic()
    try:
        data = fn()
        elapsed = round((time.monotonic() - start) * 1000)
        return {
            "label": label,
            "ok": True,
            "elapsed_ms": elapsed,
            "data": data,
            "error": None,
        }
    except Exception as exc:
        elapsed = round((time.monotonic() - start) * 1000)
        return {
            "label": label,
            "ok": False,
            "elapsed_ms": elapsed,
            "data": None,
            "error": str(exc),
        }


@staff_member_required
def integrations_overview(request):
    """Quick overview: fire one lightweight probe per integration."""
    from apps.domains.resellerclub_client import ResellerClubClient
    from apps.cloudflare_integration.services import CloudflareService
    from apps.companies.services import CompaniesHouseService
    from apps.provisioning.whm_client import WHMClient
    import stripe as stripe_module

    probes = []

    # ResellerClub — check availability of a well-known taken domain
    def _rc():
        client = ResellerClubClient()
        return client.check_availability(["google"], ["com"])

    probes.append(_probe("ResellerClub", _rc))

    # Cloudflare — verify token
    def _cf():
        import requests as _req
        token = get_runtime_setting("CLOUDFLARE_API_TOKEN", "")
        resp = _req.get(
            "https://api.cloudflare.com/client/v4/user/tokens/verify",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        return resp.json()

    probes.append(_probe("Cloudflare", _cf))

    # Companies House
    def _ch():
        svc = CompaniesHouseService()
        return svc.search_companies("test", items_per_page=1)

    probes.append(_probe("Companies House", _ch))

    # WHM
    def _whm():
        client = WHMClient()
        return client._call("version")

    probes.append(_probe("WHM / cPanel", _whm))

    # Stripe
    def _stripe():
        stripe_module.api_key = get_runtime_setting("STRIPE_SECRET_KEY", "")
        return stripe_module.Balance.retrieve()

    probes.append(_probe("Stripe", _stripe))
    def _m365():
        from apps.notifications.m365_graph import MicrosoftGraphMailClient, MicrosoftGraphMailConfig

        config = MicrosoftGraphMailConfig.load()
        if not config.enabled:
            return {"enabled": False, "status": "disabled"}
        mailbox = config.default_mailbox or config.billing_mailbox or config.support_mailbox or config.domains_mailbox
        return MicrosoftGraphMailClient(config).test_mailbox(mailbox)
    probes.append(_probe("Microsoft 365 Graph Mail", _m365))

    return render(request, "admin_tools/integrations.html", {"probes": probes})


@staff_member_required
def m365_graph_config(request):
    """Configure Microsoft Graph outbound mail and test shared mailboxes."""
    from apps.notifications.m365_graph import MicrosoftGraphMailClient, MicrosoftGraphMailConfig

    test_result = None
    if request.method == "POST":
        action = request.POST.get("action", "save")
        form = Microsoft365GraphSettingsForm(request.POST)
        if form.is_valid():
            form.save_settings()
            messages.success(request, "Microsoft 365 Graph mail settings saved.")
            config = MicrosoftGraphMailConfig.load()
            client = MicrosoftGraphMailClient(config)

            if action == "test_connectivity":
                try:
                    mailbox = (
                        config.default_mailbox
                        or config.billing_mailbox
                        or config.support_mailbox
                        or config.domains_mailbox
                        or config.notifications_mailbox
                    )
                    test_result = {"ok": True, "data": client.test_mailbox(mailbox)}
                    messages.success(request, f"Connected to Microsoft Graph mailbox: {mailbox}")
                except Exception as exc:
                    test_result = {"ok": False, "error": str(exc)}
                    messages.error(request, f"Microsoft Graph connectivity failed: {exc}")
            elif action == "send_test":
                recipient = form.cleaned_data.get("test_recipient")
                if not recipient:
                    messages.error(request, "Enter a test recipient before sending a test message.")
                else:
                    try:
                        mailbox = config.mailbox_for("notifications")
                        client.send_mail(
                            mailbox=mailbox,
                            message={
                                "subject": "CyberAsk Microsoft 365 Graph mail test",
                                "body": {
                                    "contentType": "HTML",
                                    "content": "<p>This is a test email from the CyberAsk portal Microsoft Graph integration.</p>",
                                },
                                "toRecipients": [{"emailAddress": {"address": recipient}}],
                            },
                        )
                        test_result = {"ok": True, "data": {"mailbox": mailbox, "recipient": recipient}}
                        messages.success(request, f"Test email sent to {recipient} from {mailbox}.")
                    except Exception as exc:
                        test_result = {"ok": False, "error": str(exc)}
                        messages.error(request, f"Microsoft Graph test email failed: {exc}")
            if action == "save":
                return redirect("admin_tools:m365_graph_config")
    else:
        form = Microsoft365GraphSettingsForm(initial=Microsoft365GraphSettingsForm.initial_from_settings())

    return render(
        request,
        "admin_tools/m365_graph_config.html",
        {
            "form": form,
            "test_result": test_result,
            "graph_docs_url": "https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0",
            "mailbox_scope_docs_url": "https://learn.microsoft.com/graph/auth-limit-mailbox-access",
        },
    )


def _safe_json(obj):
    """Serialise *obj* to a pretty-printed JSON string for display."""
    try:
        return json.dumps(obj, indent=2, default=str)
    except Exception:
        return str(obj)


def _dig_values(payload, keys):
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
        for value in payload.values():
            found = _dig_values(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _dig_values(item, keys)
            if found not in (None, ""):
                return found
    return ""


def _probe_summary(service: str, label: str, data):
    if not data:
        return ""

    if service == "whm" and label == "WHM version":
        version = _dig_values(data, ["version", "server_version"])
        if version:
            return f"Version: {version}"

    if service == "whm" and label == "List packages":
        packages = data.get("package") if isinstance(data, dict) else None
        if not isinstance(packages, list):
            packages = _dig_values(data, ["package", "pkg"]) if isinstance(data, dict) else None
        if isinstance(packages, list):
            return f"{len(packages)} package(s) returned"
        if isinstance(data.get("data"), dict):
            package_map = data["data"].get("pkg") or data["data"].get("package")
            if isinstance(package_map, dict):
                return f"{len(package_map)} package(s) returned"

    if service == "resellerclub" and label == "List registered domains":
        if isinstance(data, list):
            return f"{len(data)} domain order(s) returned"

    return ""


def _resellerclub_status_to_domain_status(raw_status: str) -> str:
    value = str(raw_status or "").strip().lower()
    if "expir" in value:
        return Domain.STATUS_EXPIRED
    if "suspend" in value or "hold" in value:
        return Domain.STATUS_SUSPENDED
    if "cancel" in value or "delet" in value:
        return Domain.STATUS_CANCELLED
    if "transfer" in value:
        return Domain.STATUS_TRANSFERRED
    return Domain.STATUS_ACTIVE


def _parse_iso_date(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _sync_resellerclub_inventory(include_details: bool = False, max_details: int = 100) -> dict:
    client = ResellerClubClient()
    orders = client.list_all_domain_orders(
        no_of_records=100,
        status="All",
        include_details=include_details,
        max_details=max_details,
        max_pages=50,
    )

    order_names = [str((o or {}).get("domainname") or "").strip().lower() for o in orders if isinstance(o, dict)]
    order_names = [n for n in order_names if n]
    existing_domains = {
        d.name.strip().lower(): d
        for d in Domain.objects.filter(name__in=order_names)
    }

    synced_existing = 0
    created_from_service = 0
    unmatched_external = 0

    for order in orders:
        if not isinstance(order, dict):
            continue
        if include_details and order.get("order_details"):
            order["order_details_json"] = _safe_json(order.get("order_details"))

        domain_name = str(order.get("domainname") or "").strip().lower()
        if not domain_name:
            continue

        registrar_order_id = str(order.get("orderid") or "").strip()
        expiry_dt = _parse_iso_date(order.get("expiry_date"))
        created_dt = _parse_iso_date(order.get("creation_date"))
        auto_renew = bool(order.get("recurring"))
        mapped_status = _resellerclub_status_to_domain_status(order.get("currentstatus"))

        domain_obj = existing_domains.get(domain_name)
        if domain_obj is None:
            linked_service = Service.objects.select_related("user").filter(domain_name__iexact=domain_name).first()
            if linked_service and linked_service.user:
                label, _, tld = domain_name.partition(".")
                domain_obj = Domain.objects.create(
                    user=linked_service.user,
                    name=domain_name,
                    tld=tld or label,
                    status=mapped_status,
                    registrar_id=registrar_order_id,
                    registered_at=created_dt,
                    expires_at=expiry_dt,
                    auto_renew=auto_renew,
                    dns_provider=Domain.DNS_PROVIDER_REGISTRAR,
                )
                existing_domains[domain_name] = domain_obj
                created_from_service += 1
            else:
                unmatched_external += 1
                continue
        else:
            changed_fields = []
            if registrar_order_id and domain_obj.registrar_id != registrar_order_id:
                domain_obj.registrar_id = registrar_order_id
                changed_fields.append("registrar_id")
            if expiry_dt and domain_obj.expires_at != expiry_dt:
                domain_obj.expires_at = expiry_dt
                changed_fields.append("expires_at")
            if created_dt and domain_obj.registered_at != created_dt:
                domain_obj.registered_at = created_dt
                changed_fields.append("registered_at")
            if domain_obj.status != mapped_status:
                domain_obj.status = mapped_status
                changed_fields.append("status")
            if domain_obj.auto_renew != auto_renew:
                domain_obj.auto_renew = auto_renew
                changed_fields.append("auto_renew")
            if domain_obj.dns_provider != Domain.DNS_PROVIDER_REGISTRAR:
                domain_obj.dns_provider = Domain.DNS_PROVIDER_REGISTRAR
                changed_fields.append("dns_provider")
            if changed_fields:
                domain_obj.save(update_fields=changed_fields + ["updated_at"])
            synced_existing += 1

        if registrar_order_id:
            DomainOrder.objects.filter(domain_name__iexact=domain_name).update(registrar_order_id=registrar_order_id)

    managed_domains_qs = Domain.objects.filter(dns_provider=Domain.DNS_PROVIDER_REGISTRAR)
    expiring_30d = managed_domains_qs.filter(
        expires_at__isnull=False,
        expires_at__lte=timezone.now().date() + timedelta(days=30),
    ).count()

    return {
        "domain_orders": orders,
        "domain_total": len(orders),
        "synced_existing": synced_existing,
        "created_from_service": created_from_service,
        "unmatched_external": unmatched_external,
        "managed_domain_total": managed_domains_qs.count(),
        "expiring_30d": expiring_30d,
    }


@staff_member_required
def integration_detail(request, service):
    """Detailed test view for a single integration."""
    from apps.domains.resellerclub_client import ResellerClubClient
    from apps.cloudflare_integration.services import CloudflareService
    from apps.companies.services import CompaniesHouseService
    from apps.provisioning.whm_client import WHMClient
    from apps.provisioning.models import (
        WHMAccountSnapshot,
        WHMAccountUsageSnapshot,
        WHMPackageSnapshot,
        WHMServerSnapshot,
        WHMSyncRun,
    )
    from apps.provisioning.whm_sync import WHMSyncService
    import stripe as stripe_module
    import requests as _req

    SERVICE_TESTS = {
        "resellerclub": [
            ("Check availability (google.com)", lambda: ResellerClubClient().check_availability(["google"], ["com"])),
            ("Check availability (example.com)", lambda: ResellerClubClient().check_availability(["example"], ["com"])),
            ("Get .com TLD pricing", lambda: ResellerClubClient().get_tld_pricing("com", years=1, action="registration")),
            ("List registered domains", lambda: ResellerClubClient().list_domain_orders(page_no=1, no_of_records=25, status="Active")),
        ],
        "cloudflare": [
            ("Verify token", lambda: _req.get(
                "https://api.cloudflare.com/client/v4/user/tokens/verify",
                headers={"Authorization": f"Bearer {get_runtime_setting('CLOUDFLARE_API_TOKEN', '')}"},
                timeout=10,
            ).json()),
            ("List zones (first page)", lambda: _req.get(
                "https://api.cloudflare.com/client/v4/zones?per_page=5",
                headers={"Authorization": f"Bearer {get_runtime_setting('CLOUDFLARE_API_TOKEN', '')}"},
                timeout=10,
            ).json()),
        ],
        "companies-house": [
            ("Search: 'Apple'", lambda: CompaniesHouseService().search_companies("Apple", items_per_page=3)),
            ("Company 00445790 (Apple UK)", lambda: CompaniesHouseService().get_company("00445790")),
        ],
        "whm": [
            ("WHM version", lambda: WHMClient()._call("version")),
            ("List packages", lambda: WHMClient()._call("listpkgs")),
        ],
        "stripe": [
            ("Balance", lambda: stripe_module.Balance.retrieve().__dict__),
            ("List products (limit 3)", lambda: stripe_module.Product.list(limit=3).__dict__),
        ],
    }

    if service not in SERVICE_TESTS:
        from django.http import Http404
        raise Http404(f"Unknown integration: {service}")

    if request.method == "POST" and service == "whm":
        action = (request.POST.get("action") or "").strip()
        if action == "refresh_now":
            try:
                result = WHMSyncService().sync_all()
                messages.success(
                    request,
                    "WHM inventory refreshed: "
                    f"{result.get('package_count', 0)} packages, "
                    f"{result.get('account_count', 0)} accounts, "
                    f"{result.get('usage_count', 0)} usage snapshots.",
                )
            except Exception as exc:
                messages.error(request, f"WHM refresh failed: {exc}")
            return redirect("admin_tools:integration_detail", service="whm")
        if action == "reconcile_domains":
            try:
                from apps.provisioning.tasks import reconcile_whm_registrar_domains

                task = reconcile_whm_registrar_domains.delay()
                messages.success(
                    request,
                    f"Registrar cross-check queued in the background. Task ID: {task.id}",
                )
            except Exception as exc:
                messages.error(request, f"Could not queue WHM/ResellerClub cross-check: {exc}")
            return redirect(f"{reverse('admin_tools:integration_detail', kwargs={'service': 'whm'})}?reconcile=1")
        if action == "terminate_orphan":
            username = (request.POST.get("username") or "").strip()
            keep_dns = (request.POST.get("keep_dns") or "").strip() == "1"
            try:
                WHMSyncService().terminate_orphaned_account(username=username, keep_dns=keep_dns)
                messages.success(request, f"Terminated orphaned WHM account '{username}'.")
            except Exception as exc:
                messages.error(request, f"Could not terminate '{username}': {exc}")
            return redirect(f"{reverse('admin_tools:integration_detail', kwargs={'service': 'whm'})}?reconcile=1")
        if action == "package_create":
            try:
                name = (request.POST.get("name") or "").strip()
                if not name:
                    raise ValueError("Package name is required.")
                options = {
                    "featurelist": (request.POST.get("featurelist") or "").strip(),
                    "quota": (request.POST.get("quota") or "").strip(),
                    "bwlimit": (request.POST.get("bwlimit") or "").strip(),
                    "maxpop": (request.POST.get("maxpop") or "").strip(),
                    "maxftp": (request.POST.get("maxftp") or "").strip(),
                    "maxsql": (request.POST.get("maxsql") or "").strip(),
                    "maxsub": (request.POST.get("maxsub") or "").strip(),
                    "maxpark": (request.POST.get("maxpark") or "").strip(),
                    "maxaddon": (request.POST.get("maxaddon") or "").strip(),
                }
                WHMClient().create_package(name=name, options=options)
                WHMSyncService().sync_all()
                messages.success(request, f"WHM package '{name}' created and synced.")
            except Exception as exc:
                messages.error(request, f"WHM package create failed: {exc}")
            return redirect("admin_tools:integration_detail", service="whm")
        if action == "package_update":
            try:
                name = (request.POST.get("name") or "").strip()
                if not name:
                    raise ValueError("Package name is required.")
                options = {
                    "featurelist": (request.POST.get("featurelist") or "").strip(),
                    "quota": (request.POST.get("quota") or "").strip(),
                    "bwlimit": (request.POST.get("bwlimit") or "").strip(),
                    "maxpop": (request.POST.get("maxpop") or "").strip(),
                    "maxftp": (request.POST.get("maxftp") or "").strip(),
                    "maxsql": (request.POST.get("maxsql") or "").strip(),
                    "maxsub": (request.POST.get("maxsub") or "").strip(),
                    "maxpark": (request.POST.get("maxpark") or "").strip(),
                    "maxaddon": (request.POST.get("maxaddon") or "").strip(),
                }
                WHMClient().update_package(name=name, options=options)
                WHMSyncService().sync_all()
                messages.success(request, f"WHM package '{name}' updated and synced.")
            except Exception as exc:
                messages.error(request, f"WHM package update failed: {exc}")
            return redirect("admin_tools:integration_detail", service="whm")
        if action == "package_delete":
            try:
                name = (request.POST.get("name") or "").strip()
                if not name:
                    raise ValueError("Package name is required.")
                WHMClient().delete_package(name)
                WHMSyncService().sync_all()
                messages.success(request, f"WHM package '{name}' deleted and synced.")
            except Exception as exc:
                messages.error(request, f"WHM package delete failed: {exc}")
            return redirect("admin_tools:integration_detail", service="whm")

    if request.method == "POST" and service == "resellerclub":
        action = (request.POST.get("action") or "").strip()
        if action == "refresh_now":
            try:
                sync_info = _sync_resellerclub_inventory(include_details=True, max_details=100)
                messages.success(
                    request,
                    "ResellerClub sync complete: "
                    f"{sync_info['domain_total']} fetched, "
                    f"{sync_info['synced_existing']} updated, "
                    f"{sync_info['created_from_service']} created from service mapping, "
                    f"{sync_info['unmatched_external']} unmatched external.",
                )
            except Exception as exc:
                messages.error(request, f"ResellerClub refresh failed: {exc}")
            return redirect("admin_tools:integration_detail", service="resellerclub")

    # Set Stripe API key before probe
    stripe_module.api_key = get_runtime_setting("STRIPE_SECRET_KEY", "")

    tests = []
    for label, fn in SERVICE_TESTS[service]:
        probe = _probe(label, fn)
        probe["json"] = _safe_json(probe["data"])
        probe["summary"] = _probe_summary(service, label, probe["data"])
        tests.append(probe)

    SERVICE_LABELS = {
        "resellerclub": "ResellerClub",
        "cloudflare": "Cloudflare",
        "companies-house": "Companies House",
        "whm": "WHM / cPanel",
        "stripe": "Stripe",
    }

    whm_context = None
    resellerclub_context = None
    if service == "whm":
        account_query = (request.GET.get("q") or "").strip().lower()
        suspended_filter = (request.GET.get("suspended") or "all").strip().lower()
        show_reconciliation = (request.GET.get("reconcile") or "").strip() == "1"

        def _to_float(value):
            try:
                text = str(value or "").strip().lower().replace("mb", "").replace(",", "")
                if text in {"", "unlimited", "inf", "infinity"}:
                    return None
                return float(text)
            except Exception:
                return None

        latest_run = WHMSyncRun.objects.order_by("-started_at").first()
        latest_server = WHMServerSnapshot.objects.order_by("-synced_at").first()
        packages = list(WHMPackageSnapshot.objects.filter(is_active=True).order_by("name")[:100])
        accounts_qs = WHMAccountSnapshot.objects.filter(is_active=True).select_related("service").order_by("username")
        if account_query:
            accounts_qs = accounts_qs.filter(Q(username__icontains=account_query) | Q(domain__icontains=account_query))
        if suspended_filter == "yes":
            accounts_qs = accounts_qs.filter(suspended=True)
        elif suspended_filter == "no":
            accounts_qs = accounts_qs.filter(suspended=False)

        accounts = list(accounts_qs[:200])
        usage_map = {
            u.account_id: u
            for u in WHMAccountUsageSnapshot.objects.filter(account__in=accounts).select_related("account")
        }
        for account in accounts:
            account.usage_snapshot = usage_map.get(account.id)
            account.status_label = "Suspended" if account.suspended else "Active"

        accounts_by_plan = {}
        for account in WHMAccountSnapshot.objects.filter(is_active=True).only("plan"):
            plan_key = str(account.plan or "").strip()
            if not plan_key:
                continue
            accounts_by_plan[plan_key] = accounts_by_plan.get(plan_key, 0) + 1

        usage_by_plan = {}
        active_usage = WHMAccountUsageSnapshot.objects.select_related("account").filter(account__is_active=True)
        for usage in active_usage:
            plan_key = str(getattr(usage.account, "plan", "") or "").strip()
            if not plan_key:
                continue
            stats = usage_by_plan.setdefault(plan_key, {"disk_used": 0.0, "bw_used": 0.0})
            disk_val = _to_float(usage.disk_used_mb)
            bw_val = _to_float(usage.monthly_bandwidth_used_mb)
            if disk_val is not None:
                stats["disk_used"] += disk_val
            if bw_val is not None:
                stats["bw_used"] += bw_val

        for pkg in packages:
            sold = accounts_by_plan.get(pkg.name, 0)
            pkg.accounts_sold = sold
            disk_quota = _to_float(pkg.disk_quota_mb)
            bw_quota = _to_float(pkg.bandwidth_quota_mb)
            pkg.total_allocated_disk_mb = round((disk_quota or 0.0) * sold, 2) if disk_quota is not None else None
            pkg.total_allocated_bw_mb = round((bw_quota or 0.0) * sold, 2) if bw_quota is not None else None
            usage_stats = usage_by_plan.get(pkg.name, {"disk_used": 0.0, "bw_used": 0.0})
            pkg.total_actual_disk_used_mb = round(usage_stats["disk_used"], 2)
            pkg.total_actual_bw_used_mb = round(usage_stats["bw_used"], 2)
            pkg.disk_over_allocated = bool(
                pkg.total_allocated_disk_mb is not None and pkg.total_actual_disk_used_mb > pkg.total_allocated_disk_mb
            )
            pkg.bw_over_allocated = bool(
                pkg.total_allocated_bw_mb is not None and pkg.total_actual_bw_used_mb > pkg.total_allocated_bw_mb
            )

        whm_context = {
            "latest_run": latest_run,
            "latest_server": latest_server,
            "packages": packages,
            "accounts": accounts,
            "package_total": WHMPackageSnapshot.objects.filter(is_active=True).count(),
            "account_total": WHMAccountSnapshot.objects.filter(is_active=True).count(),
            "usage_total": WHMAccountUsageSnapshot.objects.count(),
            "filters": {
                "q": account_query,
                "suspended": suspended_filter,
            },
            "reconciliation": None,
            "show_reconciliation": show_reconciliation,
        }

        if show_reconciliation:
            latest_reconciliation_run = (
                WHMSyncRun.objects.exclude(result_data__domain_reconciliation__isnull=True)
                .order_by("-started_at")
                .first()
            )
            if latest_reconciliation_run:
                whm_context["reconciliation"] = (latest_reconciliation_run.result_data or {}).get("domain_reconciliation")
                whm_context["reconciliation_run"] = latest_reconciliation_run

    if service == "resellerclub":
        full_refresh = (request.GET.get("full") or "").strip() == "1"
        try:
            sync_info = _sync_resellerclub_inventory(include_details=full_refresh, max_details=100)
            domain_orders = sync_info["domain_orders"]
        except Exception as exc:
            sync_info = {
                "domain_total": 0,
                "synced_existing": 0,
                "created_from_service": 0,
                "unmatched_external": 0,
                "managed_domain_total": Domain.objects.filter(dns_provider=Domain.DNS_PROVIDER_REGISTRAR).count(),
                "expiring_30d": Domain.objects.filter(
                    dns_provider=Domain.DNS_PROVIDER_REGISTRAR,
                    expires_at__isnull=False,
                    expires_at__lte=timezone.now().date() + timedelta(days=30),
                ).count(),
            }
            domain_orders = []
            messages.error(request, f"Could not load registrar domain list: {exc}")

        resellerclub_context = {
            "domain_orders": domain_orders,
            "domain_total": sync_info["domain_total"],
            "synced_existing": sync_info["synced_existing"],
            "created_from_service": sync_info["created_from_service"],
            "unmatched_external": sync_info["unmatched_external"],
            "managed_domain_total": sync_info["managed_domain_total"],
            "expiring_30d": sync_info["expiring_30d"],
            "full_refresh": full_refresh,
        }

    return render(request, "admin_tools/integration_detail.html", {
        "service": service,
        "service_label": SERVICE_LABELS[service],
        "tests": tests,
        "whm_context": whm_context,
        "resellerclub_context": resellerclub_context,
    })


@staff_member_required
def resellerclub_debug(request):
    """Low-level debug page to inspect full ResellerClub HTTP request/response details."""
    base_url = get_runtime_setting("RESELLERCLUB_API_URL", "https://httpapi.com/api").rstrip("/")
    reseller_id = get_runtime_setting("RESELLERCLUB_RESELLER_ID", "")
    api_key = get_runtime_setting("RESELLERCLUB_API_KEY", "")

    debug_mode = str(get_runtime_setting("RESELLERCLUB_DEBUG_MODE", "false")).strip().lower() in ("1", "true", "yes", "on")

    context = {
        "base_url": base_url,
        "endpoint": "domains/available.json",
        "domain_label": "example",
        "tlds": "com,net",
        "raw_params": "",
        "debug": None,
        "debug_mode": debug_mode,
    }

    if request.method == "POST":
        if (request.POST.get("action") or "").strip() == "toggle_debug_mode":
            from apps.admin_tools.models import IntegrationSetting

            enabled = request.POST.get("enable_debug_mode") == "on"
            IntegrationSetting.set_value(
                "RESELLERCLUB_DEBUG_MODE",
                "true" if enabled else "false",
                is_secret=False,
            )
            messages.success(request, f"ResellerClub debug mode {'enabled' if enabled else 'disabled'}.")
            return redirect(reverse("admin_tools:resellerclub_debug"))

        endpoint = (request.POST.get("endpoint") or "domains/available.json").strip().lstrip("/")
        domain_label = (request.POST.get("domain_label") or "example").strip().lower()
        tlds_raw = (request.POST.get("tlds") or "com").strip().lower()
        raw_params = (request.POST.get("raw_params") or "").strip()

        params = {
            "auth-userid": reseller_id,
            "api-key": api_key,
        }

        if raw_params:
            try:
                parsed = json.loads(raw_params)
                if not isinstance(parsed, dict):
                    raise ValueError("Raw params JSON must be an object.")
                params.update(parsed)
            except Exception as exc:
                messages.error(request, f"Invalid raw params JSON: {exc}")
                context.update(
                    {
                        "endpoint": endpoint,
                        "domain_label": domain_label,
                        "tlds": tlds_raw,
                        "raw_params": raw_params,
                    }
                )
                return render(request, "admin_tools/resellerclub_debug.html", context)
        elif endpoint in {"domains/available", "domains/available.json"}:
            tld_list = [x.strip().lstrip(".") for x in tlds_raw.split(",") if x.strip()]
            params.update(
                {
                    "domain-name": domain_label.split(".", 1)[0],
                    # LogicBoxes expects repeated tlds params (tlds=com&tlds=net),
                    # not a single comma-joined value.
                    "tlds": tld_list,
                }
            )

        session = requests.Session()
        req = requests.Request(
            "GET",
            f"{base_url}/{endpoint}",
            params=params,
            headers={"Accept": "application/json"},
        )
        prepared = session.prepare_request(req)

        debug = {
            "request": {
                "method": prepared.method,
                "url": prepared.url,
                "headers": dict(prepared.headers),
                "body": prepared.body.decode("utf-8", errors="replace")
                if isinstance(prepared.body, bytes)
                else (prepared.body or ""),
                "params": params,
            },
            "response": None,
            "error": None,
        }

        try:
            started = time.monotonic()
            response = session.send(prepared, timeout=(10, 30))
            elapsed_ms = round((time.monotonic() - started) * 1000)
            body_text = response.text
            body_json = None
            try:
                body_json = response.json()
            except Exception:
                body_json = None

            debug["response"] = {
                "elapsed_ms": elapsed_ms,
                "status_code": response.status_code,
                "reason": response.reason,
                "headers": dict(response.headers),
                "text": body_text,
                "json": body_json,
            }
        except Exception as exc:
            debug["error"] = str(exc)

        context.update(
            {
                "endpoint": endpoint,
                "domain_label": domain_label,
                "tlds": tlds_raw,
                "raw_params": raw_params,
                "debug": debug,
            }
        )

    return render(request, "admin_tools/resellerclub_debug.html", context)


# ---------------------------------------------------------------------------
# Users overview
# ---------------------------------------------------------------------------


def _normalize_domain_name(value: str) -> str:
    return str(value or "").strip().lower().rstrip(".")


def _split_domain_name(domain_name: str) -> tuple[str, str]:
    normalized = _normalize_domain_name(domain_name)
    if "." not in normalized:
        return normalized, ""
    label, tld = normalized.split(".", 1)
    return label, tld


def _safe_import_email(raw_email: str, username: str, domain_name: str) -> str:
    candidate = str(raw_email or "").strip().lower()
    if not candidate and domain_name:
        candidate = f"{username}@{domain_name}"
    if not candidate:
        candidate = f"whm-{username}@local.invalid"

    try:
        validate_email(candidate)
    except ValidationError:
        candidate = f"whm-{username}@local.invalid"

    existing = User.objects.filter(email__iexact=candidate).first()
    if existing:
        return existing.email

    if not User.objects.filter(email__iexact=candidate).exists():
        return candidate

    local, _, domain = candidate.partition("@")
    suffix = 1
    while True:
        alt = f"{local}+whm{suffix}@{domain or 'local.invalid'}"
        if not User.objects.filter(email__iexact=alt).exists():
            return alt
        suffix += 1


def _ensure_import_packages_from_whm_snapshots() -> list[Package]:
    """Ensure there are active local packages available for WHM import mapping."""
    def _to_int(value) -> int:
        try:
            return int(str(value or "0").strip())
        except Exception:
            return 0

    active_packages = list(Package.objects.filter(is_active=True).order_by("id"))
    if active_packages:
        return active_packages

    whm_pkg_model = Service._meta.apps.get_model("provisioning", "WHMPackageSnapshot")
    snapshots = list(whm_pkg_model.objects.filter(is_active=True).order_by("name"))
    for snap in snapshots:
        raw_name = str(getattr(snap, "name", "") or "").strip()
        if not raw_name:
            continue
        base_slug = slugify(raw_name) or f"whm-{raw_name.lower()}"
        slug = base_slug
        suffix = 2
        while Package.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1

        package_obj = Package.objects.create(
            name=raw_name,
            slug=slug[:50],
            price_monthly="0.00",
            price_annually="0.00",
            whm_package_name=raw_name,
            disk_quota_mb=_to_int(getattr(snap, "disk_quota_mb", "0")),
            bandwidth_mb=_to_int(getattr(snap, "bandwidth_quota_mb", "0")),
            email_accounts=_to_int(getattr(snap, "max_email_accounts", "0")),
            databases=_to_int(getattr(snap, "max_databases", "0")),
            is_active=True,
        )
        active_packages.append(package_obj)

    if not active_packages:
        raise ValueError(
            "No active packages and no WHM package snapshots available. Run WHM refresh first or create a package manually."
        )

    return active_packages


def _sync_whm_import() -> dict:
    sync_result = WHMSyncService().sync_all()
    snapshots = Service._meta.apps.get_model("provisioning", "WHMAccountSnapshot").objects.filter(is_active=True)
    active_packages = _ensure_import_packages_from_whm_snapshots()
    default_package = active_packages[0]

    rc_client = None
    reseller_index = None
    stats = {
        "accounts_seen": snapshots.count(),
        "users_created": 0,
        "domains_created": 0,
        "services_created": 0,
        "services_updated": 0,
        "managed_domains_linked": 0,
        "warnings": [],
        "sync_result": sync_result,
    }

    for acct in snapshots:
        username = str(acct.username or "").strip()
        domain_name = _normalize_domain_name(acct.domain)
        if not username:
            continue

        email = _safe_import_email(acct.email, username, domain_name)
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            user = User.objects.create_user(
                email=email,
                password=None,
                first_name=username[:150],
            )
            stats["users_created"] += 1

        if domain_name:
            domain_obj = Domain.objects.filter(name__iexact=domain_name).first()
            if domain_obj is None:
                managed_order = None
                label, tld = _split_domain_name(domain_name)
                if label and tld:
                    try:
                        rc_client = rc_client or ResellerClubClient()
                        availability = rc_client.check_availability([label], [tld])
                        availability_info = availability.get(domain_name, {}) if isinstance(availability, dict) else {}
                        availability_status = str((availability_info or {}).get("status", "")).strip().lower()

                        if availability_status != "available":
                            if reseller_index is None:
                                reseller_index = {}
                                orders = rc_client.list_all_domain_orders(no_of_records=100, status="All", max_pages=50)
                                for order in orders:
                                    order_domain = _normalize_domain_name(order.get("domainname"))
                                    if order_domain:
                                        reseller_index[order_domain] = order
                            managed_order = reseller_index.get(domain_name)
                    except Exception as exc:
                        stats["warnings"].append(f"{domain_name}: reseller lookup skipped ({exc})")

                _, tld_value = _split_domain_name(domain_name)
                domain_defaults = {
                    "user": user,
                    "tld": tld_value,
                    "status": Domain.STATUS_ACTIVE,
                    "dns_provider": Domain.DNS_PROVIDER_EXTERNAL,
                    "auto_renew": True,
                }
                if managed_order:
                    domain_defaults["dns_provider"] = Domain.DNS_PROVIDER_REGISTRAR
                    order_id = managed_order.get("orderid")
                    if order_id not in (None, ""):
                        domain_defaults["registrar_id"] = str(order_id)
                    stats["managed_domains_linked"] += 1

                Domain.objects.create(name=domain_name, **domain_defaults)
                stats["domains_created"] += 1

        package = Package.objects.filter(is_active=True, whm_package_name__iexact=acct.plan).first()
        if package is None:
            package = Package.objects.filter(is_active=True, name__iexact=acct.plan).first() or default_package

        service = Service.objects.filter(cpanel_username__iexact=username).first()
        if service is None and domain_name:
            service = Service.objects.filter(user=user, domain_name__iexact=domain_name).first()

        defaults = {
            "user": user,
            "package": package,
            "status": Service.STATUS_ACTIVE,
            "domain_name": domain_name,
            "cpanel_domain": domain_name,
            "cpanel_ip": acct.ip or None,
            "cpanel_server": acct.server or "",
        }
        if service is None:
            Service.objects.create(cpanel_username=username, **defaults)
            stats["services_created"] += 1
        else:
            changed_fields = []
            for key, value in defaults.items():
                if getattr(service, key) != value:
                    setattr(service, key, value)
                    changed_fields.append(key)
            if service.cpanel_username != username:
                service.cpanel_username = username
                changed_fields.append("cpanel_username")
            if changed_fields:
                service.save(update_fields=changed_fields + ["updated_at"])
                stats["services_updated"] += 1

    return stats


def _sync_whm_export() -> dict:
    client = WHMClient()
    eligible_services = (
        Service.objects.select_related("user", "package")
        .filter(cpanel_username="")
        .exclude(domain_name="")
    )
    created = 0
    failed = 0
    warnings = []

    for service in eligible_services:
        package_name = service.package.whm_package_name or service.package.name
        if not package_name:
            failed += 1
            warnings.append(f"Service #{service.pk}: package has no WHM package mapping")
            continue

        username = generate_cpanel_username(service.domain_name or service.user.email.split("@")[0])
        password = generate_secure_password()
        try:
            client.create_account(
                domain=service.domain_name,
                username=username,
                password=password,
                package=package_name,
                email=service.user.email,
            )
            service.cpanel_username = username
            service.status = Service.STATUS_ACTIVE
            service.save(update_fields=["cpanel_username", "status", "updated_at"])
            created += 1
        except Exception as exc:
            failed += 1
            warnings.append(f"Service #{service.pk}: {exc}")

    return {
        "eligible": eligible_services.count(),
        "created": created,
        "failed": failed,
        "warnings": warnings,
    }

@staff_member_required
def users(request):
    """Paginated user list with search."""
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "sync_whm":
            direction = (request.POST.get("direction") or "").strip().lower()
            if direction not in {"import", "export"}:
                messages.error(request, "Choose a valid sync direction: import or export.")
                return redirect("admin_tools:users")

            try:
                if direction == "import":
                    result = _sync_whm_import()
                    messages.success(
                        request,
                        (
                            "WHM import complete. "
                            f"Accounts seen: {result['accounts_seen']}, users created: {result['users_created']}, "
                            f"domains created: {result['domains_created']}, services created: {result['services_created']}, "
                            f"services updated: {result['services_updated']}, managed domains linked: {result['managed_domains_linked']}."
                        ),
                    )
                    for warning in result.get("warnings", [])[:5]:
                        messages.warning(request, warning)
                else:
                    result = _sync_whm_export()
                    messages.success(
                        request,
                        (
                            "WHM export complete. "
                            f"Eligible services: {result['eligible']}, created in WHM: {result['created']}, failed: {result['failed']}."
                        ),
                    )
                    for warning in result.get("warnings", [])[:5]:
                        messages.warning(request, warning)
            except Exception as exc:
                messages.error(request, f"WHM sync failed: {exc}")

            return redirect("admin_tools:users")

    q = request.GET.get("q", "").strip()
    qs = User.objects.select_related("business_profile").order_by("-created_at")
    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(full_name__icontains=q))

    paginator = Paginator(qs, 30)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "admin_tools/users.html", {
        "page_obj": page,
        "search_q": q,
        "total_users": User.objects.count(),
        "staff_count": User.objects.filter(is_staff=True).count(),
        "superuser_count": User.objects.filter(is_superuser=True).count(),
    })


# ---------------------------------------------------------------------------
# Invoices overview
# ---------------------------------------------------------------------------

@staff_member_required
def invoices(request):
    """Paginated invoice review page with quick filters and totals."""
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    date_from = request.GET.get("from", "").strip()
    date_to = request.GET.get("to", "").strip()

    qs = Invoice.objects.select_related("user").order_by("-created_at")

    if q:
        qs = qs.filter(
            Q(number__icontains=q)
            | Q(user__email__icontains=q)
            | Q(billing_name__icontains=q)
        )

    if status:
        qs = qs.filter(status=status)

    if date_from:
        try:
            from datetime import date
            qs = qs.filter(created_at__date__gte=date.fromisoformat(date_from))
        except ValueError:
            pass

    if date_to:
        try:
            from datetime import date
            qs = qs.filter(created_at__date__lte=date.fromisoformat(date_to))
        except ValueError:
            pass

    paginator = Paginator(qs, 30)
    page = paginator.get_page(request.GET.get("page"))

    last_30 = timezone.now() - timedelta(days=30)
    stats = {
        "total_invoices": Invoice.objects.count(),
        "paid_invoices": Invoice.objects.filter(status=Invoice.STATUS_PAID).count(),
        "unpaid_invoices": Invoice.objects.filter(status__in=[Invoice.STATUS_UNPAID, Invoice.STATUS_OVERDUE]).count(),
        "revenue_30d": Invoice.objects.filter(status=Invoice.STATUS_PAID, paid_at__gte=last_30).aggregate(total=Sum("total"))["total"] or 0,
        "outstanding_total": Invoice.objects.filter(status__in=[Invoice.STATUS_UNPAID, Invoice.STATUS_OVERDUE]).aggregate(total=Sum("total"))["total"] or 0,
    }

    return render(request, "admin_tools/invoices.html", {
        "page_obj": page,
        "invoices": page.object_list,
        "search_q": q,
        "status_filter": status,
        "date_from": date_from,
        "date_to": date_to,
        "status_choices": Invoice.STATUS_CHOICES,
        "stats": stats,
    })


def _decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_tld_list(raw: str):
    parts = [
        p.strip().lower()
        for p in (raw or "").replace("\n", ",").replace("\t", ",").split(",")
        if p.strip()
    ]
    return list(dict.fromkeys(parts))


@staff_member_required
def tld_pricing(request):
    settings_obj = DomainPricingSettings.get_solo()
    debug_mode = str(get_runtime_setting("RESELLERCLUB_DEBUG_MODE", "false")).strip().lower() in ("1", "true", "yes", "on")

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        redirect_url = reverse("admin_tools:tld_pricing")

        if action == "save_settings":
            margin = _decimal_or_none(request.POST.get("default_profit_margin_percentage"))
            interval_raw = (request.POST.get("sync_interval_hours") or "").strip()
            tlds_raw = request.POST.get("supported_tlds", "")

            if margin is None:
                messages.error(request, "Default margin must be a valid number.")
                return redirect(redirect_url)

            try:
                interval = int(interval_raw)
            except ValueError:
                messages.error(request, "Sync interval must be a whole number of hours.")
                return redirect(redirect_url)

            if interval < 1 or interval > 168:
                messages.error(request, "Sync interval must be between 1 and 168 hours.")
                return redirect(redirect_url)

            parsed_tlds = _parse_tld_list(tlds_raw)
            if not parsed_tlds:
                messages.error(request, "Supported TLD list cannot be empty.")
                return redirect(redirect_url)

            settings_obj.default_profit_margin_percentage = margin
            settings_obj.sync_enabled = request.POST.get("sync_enabled") == "on"
            settings_obj.sync_interval_hours = interval
            settings_obj.supported_tlds = parsed_tlds
            settings_obj.save(
                update_fields=[
                    "default_profit_margin_percentage",
                    "sync_enabled",
                    "sync_interval_hours",
                    "supported_tlds",
                    "updated_at",
                ]
            )

            from apps.domains.tasks import ensure_tld_pricing_sync_schedule

            ensure_tld_pricing_sync_schedule(settings_obj)
            messages.success(request, "Domain pricing settings updated.")
            return redirect(redirect_url)

        if action == "import_all_tlds":
            from apps.domains.resellerclub_client import ResellerClubClient, ResellerClubError
            from apps.domains.tasks import ensure_tld_pricing_sync_schedule

            try:
                imported_tlds = ResellerClubClient().list_available_tlds()
            except ResellerClubError as exc:
                messages.error(request, f"Could not import TLD list: {exc}")
                return redirect(redirect_url)

            if not imported_tlds:
                messages.error(request, "Registrar returned no TLDs to import.")
                return redirect(redirect_url)

            settings_obj.supported_tlds = imported_tlds
            settings_obj.save(update_fields=["supported_tlds", "updated_at"])
            ensure_tld_pricing_sync_schedule(settings_obj)

            from apps.domains.pricing import TLDPricingService
            from django.utils import timezone

            settings_obj.last_sync_started_at = timezone.now()
            settings_obj.last_sync_error = ""
            settings_obj.save(update_fields=["last_sync_started_at", "last_sync_error", "updated_at"])
            try:
                synced = TLDPricingService().sync_pricing(tlds=imported_tlds)
                settings_obj.last_sync_completed_at = timezone.now()
                settings_obj.last_sync_error = ""
                settings_obj.save(update_fields=["last_sync_completed_at", "last_sync_error", "updated_at"])
                mode_note = " (debug mode)" if debug_mode else ""
                messages.success(
                    request,
                    f"Imported {len(imported_tlds)} TLDs and updated {len(synced)} pricing record(s) inline{mode_note}.",
                )
            except Exception as exc:
                settings_obj.last_sync_error = str(exc)
                settings_obj.save(update_fields=["last_sync_error", "updated_at"])
                messages.error(request, f"Imported TLD list but inline sync failed: {exc}")

        if action == "sync_all":
            tlds = list(settings_obj.supported_tlds or [])
            from apps.domains.pricing import TLDPricingService
            from django.utils import timezone

            settings_obj.last_sync_started_at = timezone.now()
            settings_obj.last_sync_error = ""
            settings_obj.save(update_fields=["last_sync_started_at", "last_sync_error", "updated_at"])
            try:
                synced = TLDPricingService().sync_pricing(tlds=tlds)
                settings_obj.last_sync_completed_at = timezone.now()
                settings_obj.last_sync_error = ""
                settings_obj.save(update_fields=["last_sync_completed_at", "last_sync_error", "updated_at"])
                mode_note = " (debug mode)" if debug_mode else ""
                messages.success(
                    request,
                    f"Ran pricing sync inline{mode_note}. Updated {len(synced)} TLD record(s)."
                    + (" Check the debug tray for request details." if debug_mode else ""),
                )
            except Exception as exc:
                settings_obj.last_sync_error = str(exc)
                settings_obj.save(update_fields=["last_sync_error", "updated_at"])
                messages.error(request, f"Inline pricing sync failed: {exc}")

        if action == "sync_tld":
            tld = (request.POST.get("tld") or "").strip().lower()
            if not tld:
                messages.error(request, "No TLD selected for sync.")
                return redirect(redirect_url)
            from apps.domains.pricing import TLDPricingService
            from django.utils import timezone

            settings_obj.last_sync_started_at = timezone.now()
            settings_obj.last_sync_error = ""
            settings_obj.save(update_fields=["last_sync_started_at", "last_sync_error", "updated_at"])
            try:
                synced = TLDPricingService().sync_pricing(tlds=[tld])
                settings_obj.last_sync_completed_at = timezone.now()
                settings_obj.last_sync_error = ""
                settings_obj.save(update_fields=["last_sync_completed_at", "last_sync_error", "updated_at"])
                mode_note = " (debug mode)" if debug_mode else ""
                messages.success(
                    request,
                    f"Ran pricing sync inline for .{tld}{mode_note}. Updated {len(synced)} record(s)."
                    + (" Check the debug tray." if debug_mode else ""),
                )
            except Exception as exc:
                settings_obj.last_sync_error = str(exc)
                settings_obj.save(update_fields=["last_sync_error", "updated_at"])
                messages.error(request, f"Inline pricing sync failed for .{tld}: {exc}")

        if action == "save_tld":
            tld = (request.POST.get("tld") or "").strip().lower()
            try:
                obj = TLDPricing.objects.get(tld=tld)
            except TLDPricing.DoesNotExist:
                messages.error(request, f"Unknown TLD record: {tld}")
                return redirect(redirect_url)

            reg_cost = _decimal_or_none(request.POST.get("registration_cost"))
            ren_cost = _decimal_or_none(request.POST.get("renewal_cost"))
            trf_cost = _decimal_or_none(request.POST.get("transfer_cost"))
            margin_raw = request.POST.get("profit_margin_percentage", "")
            margin_override = _decimal_or_none(margin_raw) if margin_raw != "" else None

            if reg_cost is None or ren_cost is None or trf_cost is None:
                messages.error(request, f"Costs for .{tld} must be valid numbers.")
                return redirect(redirect_url)

            obj.currency = ((request.POST.get("currency") or obj.currency or "GBP").upper())[:3]
            obj.registration_cost = reg_cost
            obj.renewal_cost = ren_cost
            obj.transfer_cost = trf_cost
            obj.profit_margin_percentage = margin_override
            obj.is_active = request.POST.get("is_active") == "on"
            obj.save(
                update_fields=[
                    "currency",
                    "registration_cost",
                    "renewal_cost",
                    "transfer_cost",
                    "profit_margin_percentage",
                    "is_active",
                    "updated_at",
                ]
            )
            messages.success(request, f"Updated pricing for .{tld}.")
            return redirect(redirect_url)

    search_q = (request.GET.get("q") or "").strip().lower()
    status_filter = (request.GET.get("status") or "").strip().lower()
    loss_filter = request.GET.get("loss") == "1"

    qs = TLDPricing.objects.order_by("tld")
    if search_q:
        qs = qs.filter(tld__icontains=search_q)
    if status_filter == "active":
        qs = qs.filter(is_active=True)
    elif status_filter == "inactive":
        qs = qs.filter(is_active=False)

    rows = []
    for obj in qs:
        reg_price = obj.registration_price
        ren_price = obj.renewal_price
        trf_price = obj.transfer_price
        reg_loss = reg_price < obj.registration_cost
        ren_loss = ren_price < obj.renewal_cost
        trf_loss = trf_price < obj.transfer_cost
        any_loss = reg_loss or ren_loss or trf_loss
        if loss_filter and not any_loss:
            continue

        rows.append(
            {
                "obj": obj,
                "margin": obj.effective_profit_margin_percentage,
                "registration_price": reg_price,
                "renewal_price": ren_price,
                "transfer_price": trf_price,
                "registration_loss": reg_loss,
                "renewal_loss": ren_loss,
                "transfer_loss": trf_loss,
                "any_loss": any_loss,
            }
        )

    all_rows = []
    for obj in TLDPricing.objects.all():
        all_rows.append(
            obj.registration_price < obj.registration_cost
            or obj.renewal_price < obj.renewal_cost
            or obj.transfer_price < obj.transfer_cost
        )

    stats = {
        "total": TLDPricing.objects.count(),
        "active": TLDPricing.objects.filter(is_active=True).count(),
        "inactive": TLDPricing.objects.filter(is_active=False).count(),
        "loss_count": sum(1 for x in all_rows if x),
        "never_synced": TLDPricing.objects.filter(last_synced_at__isnull=True).count(),
    }

    return render(
        request,
        "admin_tools/tld_pricing.html",
        {
            "settings_obj": settings_obj,
            "rows": rows,
            "search_q": search_q,
            "status_filter": status_filter,
            "loss_filter": loss_filter,
            "stats": stats,
            "supported_tlds_text": ", ".join(settings_obj.supported_tlds or []),
        },
    )


# ---------------------------------------------------------------------------
# Security & Audit log
# ---------------------------------------------------------------------------

@staff_member_required
def security(request):
    """Security overview: audit log, email log, session stats."""
    q = request.GET.get("q", "").strip()
    date_from = request.GET.get("from", "")
    date_to = request.GET.get("to", "")

    audit_qs = AuditLog.objects.select_related("user").order_by("-created_at")
    if q:
        audit_qs = audit_qs.filter(Q(action__icontains=q) | Q(user__email__icontains=q) | Q(ip_address__icontains=q))
    if date_from:
        try:
            from datetime import date
            d = date.fromisoformat(date_from)
            audit_qs = audit_qs.filter(created_at__date__gte=d)
        except ValueError:
            pass
    if date_to:
        try:
            from datetime import date
            d = date.fromisoformat(date_to)
            audit_qs = audit_qs.filter(created_at__date__lte=d)
        except ValueError:
            pass

    paginator = Paginator(audit_qs, 40)
    page = paginator.get_page(request.GET.get("page"))

    email_logs = EmailLog.objects.order_by("-created_at")[:30]

    last_24h = timezone.now() - timedelta(hours=24)
    stats = {
        "total_audit_records": AuditLog.objects.count(),
        "audit_last_24h": AuditLog.objects.filter(created_at__gte=last_24h).count(),
        "email_total": EmailLog.objects.count(),
        "email_errors": EmailLog.objects.filter(status="error").count(),
        "active_staff": User.objects.filter(is_staff=True, is_active=True).count(),
        "superusers": User.objects.filter(is_superuser=True).count(),
    }

    return render(request, "admin_tools/security.html", {
        "page_obj": page,
        "search_q": q,
        "date_from": date_from,
        "date_to": date_to,
        "email_logs": email_logs,
        "stats": stats,
    })


# ---------------------------------------------------------------------------
# Database stats
# ---------------------------------------------------------------------------

@staff_member_required
def database(request):
    """Database table row counts and basic stats."""
    from django.db import connection

    table_stats = []
    try:
        with connection.cursor() as cursor:
            table_names = sorted(connection.introspection.table_names(cursor))
            for table in table_names:
                try:
                    cursor.execute(f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608
                    count = cursor.fetchone()[0]
                except Exception:
                    count = "—"
                table_stats.append({"table": table, "rows": count})
    except Exception as exc:
        table_stats = []
        messages.error(request, f"Could not read database stats: {exc}")

    # DB engine info
    db_cfg = settings.DATABASES.get("default", {})
    db_engine = db_cfg.get("ENGINE", "").split(".")[-1]
    db_name = db_cfg.get("NAME", "")

    return render(request, "admin_tools/database.html", {
        "table_stats": table_stats,
        "db_engine": db_engine,
        "db_name": db_name,
        "total_tables": len(table_stats),
    })


# ---------------------------------------------------------------------------
# Settings overview
# ---------------------------------------------------------------------------

_SECRET_KEYS = {
    "SECRET_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PUBLISHABLE_KEY", "RESELLERCLUB_API_KEY", "CLOUDFLARE_API_TOKEN",
    "COMPANIES_HOUSE_API_KEY", "WHM_API_TOKEN", "GOCARDLESS_ACCESS_TOKEN",
    "GOCARDLESS_WEBHOOK_SECRET", "PAYPAL_CLIENT_SECRET", "EMAIL_HOST_PASSWORD",
    "DATABASE_URL", "REDIS_URL",
}


def _redact(key, value):
    if key in _SECRET_KEYS:
        return "•••••••• (redacted)"
    if isinstance(value, str) and len(value) > 120:
        return value[:120] + "…"
    return value


@staff_member_required
def settings_overview(request):
    """Show non-sensitive application configuration."""
    cfg = {
        "General": {
            "SITE_NAME": settings.SITE_NAME,
            "SITE_DOMAIN": settings.SITE_DOMAIN,
            "DEBUG": settings.DEBUG,
            "TIME_ZONE": settings.TIME_ZONE,
            "LANGUAGE_CODE": settings.LANGUAGE_CODE,
            "ALLOWED_HOSTS": ", ".join(settings.ALLOWED_HOSTS),
        },
        "Authentication": {
            "AUTH_USER_MODEL": settings.AUTH_USER_MODEL,
            "ACCOUNT_EMAIL_VERIFICATION": getattr(settings, "ACCOUNT_EMAIL_VERIFICATION", "—"),
            "SESSION_COOKIE_AGE (seconds)": settings.SESSION_COOKIE_AGE,
            "SESSION_COOKIE_HTTPONLY": settings.SESSION_COOKIE_HTTPONLY,
            "LOGIN_RATE_LIMIT_MAX_ATTEMPTS": getattr(settings, "LOGIN_RATE_LIMIT_MAX_ATTEMPTS", "—"),
            "LOGIN_RATE_LIMIT_WINDOW_SECONDS": getattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", "—"),
        },
        "Security": {
            "SECURE_CONTENT_TYPE_NOSNIFF": settings.SECURE_CONTENT_TYPE_NOSNIFF,
            "X_FRAME_OPTIONS": settings.X_FRAME_OPTIONS,
            "CSP_DEFAULT_SRC": " ".join(getattr(settings, "CSP_DEFAULT_SRC", [])),
            "CSP_SCRIPT_SRC": " ".join(getattr(settings, "CSP_SCRIPT_SRC", [])),
            "DJANGO_ADMIN_URL": _redact("DJANGO_ADMIN_URL", getattr(settings, "DJANGO_ADMIN_URL", "—")),
        },
        "Email": {
            "EMAIL_BACKEND": settings.EMAIL_BACKEND,
            "M365_GRAPH_ENABLED": get_runtime_setting("M365_GRAPH_ENABLED", getattr(settings, "M365_GRAPH_ENABLED", False)),
            "M365_GRAPH_DEFAULT_MAILBOX": get_runtime_setting("M365_GRAPH_DEFAULT_MAILBOX", ""),
            "M365_GRAPH_BILLING_MAILBOX": get_runtime_setting("M365_GRAPH_BILLING_MAILBOX", ""),
            "M365_GRAPH_SUPPORT_MAILBOX": get_runtime_setting("M365_GRAPH_SUPPORT_MAILBOX", ""),
            "M365_GRAPH_DOMAINS_MAILBOX": get_runtime_setting("M365_GRAPH_DOMAINS_MAILBOX", ""),
            "EMAIL_HOST": settings.EMAIL_HOST or "(not set)",
            "EMAIL_PORT": settings.EMAIL_PORT,
            "EMAIL_USE_TLS": settings.EMAIL_USE_TLS,
            "EMAIL_HOST_USER": settings.EMAIL_HOST_USER or "(not set)",
            "DEFAULT_FROM_EMAIL": settings.DEFAULT_FROM_EMAIL,
        },
        "Integrations": {
            "RESELLERCLUB_API_URL": getattr(settings, "RESELLERCLUB_API_URL", "—"),
            "RESELLERCLUB_RESELLER_ID": getattr(settings, "RESELLERCLUB_RESELLER_ID", "—") or "(not set)",
            "WHM_HOST": getattr(settings, "WHM_HOST", "—") or "(not set)",
            "WHM_PORT": getattr(settings, "WHM_PORT", "—"),
            "WHM_USERNAME": getattr(settings, "WHM_USERNAME", "—"),
            "CLOUDFLARE_EMAIL": getattr(settings, "CLOUDFLARE_EMAIL", "—") or "(not set)",
            "COMPANIES_HOUSE_API_KEY": "•••• (redacted)" if getattr(settings, "COMPANIES_HOUSE_API_KEY", "") else "(not set)",
            "STRIPE_PUBLISHABLE_KEY": _redact("STRIPE_PUBLISHABLE_KEY", getattr(settings, "STRIPE_PUBLISHABLE_KEY", "")),
        },
        "Celery": {
            "CELERY_BROKER_URL": settings.CELERY_BROKER_URL,
            "CELERY_RESULT_BACKEND": settings.CELERY_RESULT_BACKEND,
            "CELERY_TIMEZONE": settings.CELERY_TIMEZONE,
            "CELERY_TASK_TIME_LIMIT (s)": settings.CELERY_TASK_TIME_LIMIT,
            "CELERY_RESULT_EXPIRES (s)": settings.CELERY_RESULT_EXPIRES,
        },
        "Storage & Media": {
            "STATIC_URL": settings.STATIC_URL,
            "STATIC_ROOT": str(settings.STATIC_ROOT),
            "MEDIA_URL": settings.MEDIA_URL,
            "MEDIA_ROOT": str(settings.MEDIA_ROOT),
            "WEBSITE_TEMPLATES_ZIP_ROOT": getattr(settings, "WEBSITE_TEMPLATES_ZIP_ROOT", "—"),
            "WEBSITE_TEMPLATES_EXTRACTED_ROOT": getattr(settings, "WEBSITE_TEMPLATES_EXTRACTED_ROOT", "—"),
        },
    }

    has_companies_house_key = bool(get_runtime_setting("COMPANIES_HOUSE_API_KEY", ""))
    wizard_setting_steps = [
        {
            "key": step_key,
            "title": wizard_views.STEP_META[step_key]["title"],
            "description": wizard_views.STEP_META[step_key]["description"],
            "icon": wizard_views.STEP_META[step_key]["icon"],
        }
        for step_key in wizard_views.WizardProgress.STEPS
        if step_key != wizard_views.WizardProgress.STEP_ADMIN
    ]

    return render(
        request,
        "admin_tools/settings_overview.html",
        {
            "cfg": cfg,
            "has_companies_house_key": has_companies_house_key,
            "wizard_setting_steps": wizard_setting_steps,
        },
    )


@staff_member_required
def settings_setup_step(request, step_key: str):
    """Edit wizard-managed settings from the normal Admin Tools settings area."""
    editable_steps = [
        key
        for key in wizard_views.WizardProgress.STEPS
        if key != wizard_views.WizardProgress.STEP_ADMIN
    ]
    if step_key not in editable_steps:
        messages.error(request, f"Unknown settings section: {step_key}")
        return redirect("admin_tools:settings_overview")

    step_meta = wizard_views.STEP_META[step_key]
    form_class = step_meta["form_class"]
    connection_result = None

    if request.method == "POST":
        action = request.POST.get("action", "save")
        form = form_class(request.POST)
        if form.is_valid():
            if action == "test":
                ok, detail = wizard_views._test_connection(step_key, form.cleaned_data)
                connection_result = {"ok": ok, "detail": detail}
                if ok:
                    messages.success(request, f"Connection test passed: {detail}")
                else:
                    messages.error(request, f"Connection test failed: {detail}")
            else:
                wizard_views._process_step(step_key, form.cleaned_data, request)
                messages.success(request, f"{step_meta['title']} updated.")
                return redirect("admin_tools:settings_setup_step", step_key=step_key)
    else:
        form = form_class(initial=wizard_views._initial_for_step(step_key))

    steps = [
        {
            "key": key,
            "title": wizard_views.STEP_META[key]["title"],
            "icon": wizard_views.STEP_META[key]["icon"],
            "description": wizard_views.STEP_META[key]["description"],
            "current": key == step_key,
        }
        for key in editable_steps
    ]

    return render(
        request,
        "admin_tools/settings_setup_step.html",
        {
            "form": form,
            "step_key": step_key,
            "meta": step_meta,
            "steps": steps,
            "connection_result": connection_result,
        },
    )


@staff_member_required
def companies_house_config(request):
    from apps.admin_tools.models import IntegrationSetting
    from apps.companies.services import CompaniesHouseService

    test_number = ""
    lookup_result = None
    key_value = get_runtime_setting("COMPANIES_HOUSE_API_KEY", "")

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "save_key":
            new_key = (request.POST.get("companies_house_api_key") or "").strip()
            if not new_key:
                messages.error(request, "API key cannot be empty.")
            else:
                IntegrationSetting.set_value(
                    "COMPANIES_HOUSE_API_KEY",
                    new_key,
                    is_secret=True,
                )
                key_value = new_key
                messages.success(request, "Companies House API key saved.")

        if action == "test_lookup":
            test_number = (request.POST.get("company_number") or "").strip().replace(" ", "").upper()
            if not test_number:
                messages.warning(request, "Enter a company number to test lookup.")
            else:
                lookup_result = CompaniesHouseService().get_company(test_number)
                if lookup_result:
                    messages.success(request, f"Lookup succeeded for company {test_number}.")
                else:
                    messages.error(
                        request,
                        "Lookup failed. Check the API key and company number, then try again.",
                    )

    return render(
        request,
        "admin_tools/companies_house_config.html",
        {
            "has_api_key": bool(key_value),
            "api_key_hint": "Configured" if key_value else "Not configured",
            "test_number": test_number,
            "lookup_result": lookup_result,
        },
    )


# ---------------------------------------------------------------------------
# Setup wizard alias (redirect to wizard_index)
# ---------------------------------------------------------------------------

@staff_member_required
def setup(request):
    return redirect(reverse("admin_tools:wizard_index"))


# ---------------------------------------------------------------------------
# Phase 4 Observability: Email log, Webhook log, Audit log
# ---------------------------------------------------------------------------


@staff_member_required
def email_log(request):
    """View all outgoing email attempts (EmailLog)."""
    qs = EmailLog.objects.order_by("-created_at")

    query = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    if query:
        qs = qs.filter(recipient__icontains=query)
    if status:
        qs = qs.filter(status=status)

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "admin_tools/email_log.html", {
        "page_obj": page_obj,
        "query": query,
        "status": status,
    })


@staff_member_required
def webhook_log(request):
    """View incoming webhook events (WebhookEvent)."""
    from apps.payments.models import WebhookEvent
    qs = WebhookEvent.objects.order_by("-created_at")

    provider = (request.GET.get("provider") or "").strip()
    event_type = (request.GET.get("event_type") or "").strip()
    unprocessed_only = request.GET.get("unprocessed") == "1"

    if provider:
        qs = qs.filter(provider__icontains=provider)
    if event_type:
        qs = qs.filter(event_type__icontains=event_type)
    if unprocessed_only:
        qs = qs.filter(processed=False)

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "admin_tools/webhook_log.html", {
        "page_obj": page_obj,
        "provider": provider,
        "event_type": event_type,
        "unprocessed_only": unprocessed_only,
    })


@staff_member_required
def audit_log(request):
    """View the AuditLog with search/filter."""
    qs = AuditLog.objects.select_related("user").order_by("-created_at")

    query = (request.GET.get("q") or "").strip()
    action = (request.GET.get("action") or "").strip()
    model = (request.GET.get("model") or "").strip()

    if query:
        qs = qs.filter(action__icontains=query)
    if action:
        qs = qs.filter(action=action)
    if model:
        qs = qs.filter(model_name__icontains=model)

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    distinct_actions = AuditLog.objects.values_list("action", flat=True).distinct().order_by("action")

    return render(request, "admin_tools/audit_log.html", {
        "page_obj": page_obj,
        "query": query,
        "action": action,
        "model": model,
        "distinct_actions": distinct_actions,
    })


@staff_member_required
def feature_flags(request):
    """Toggle boolean feature flags (IntegrationSetting rows with FEATURE_ prefix)."""
    from apps.admin_tools.models import IntegrationSetting

    KNOWN_FLAGS = [
        ("FEATURE_DOMAIN_TRANSFERS", "Domain transfers", "Allow clients to initiate inbound domain transfers."),
        ("FEATURE_BULK_DOMAIN_RENEW", "Bulk domain renew", "Show bulk renew UI on My Domains."),
        ("FEATURE_ACCOUNT_STATEMENT", "Account statement", "Show account statement page for clients."),
        ("FEATURE_NOTIFICATION_PREFS", "Notification preferences", "Allow clients to manage their notification opt-outs."),
        ("FEATURE_BLOG", "Public blog", "Show the blog on the public site."),
        ("FEATURE_TESTIMONIALS", "Testimonials", "Show testimonials on the public site."),
    ]

    if request.method == "POST":
        for key, _label, _desc in KNOWN_FLAGS:
            value = "true" if request.POST.get(key) == "1" else "false"
            IntegrationSetting.objects.update_or_create(
                key=key,
                defaults={"value": value, "is_secret": False},
            )
        from django.contrib import messages
        messages.success(request, "Feature flags updated.")
        return redirect("admin_tools:feature_flags")

    db_values = {s.key: s.value for s in IntegrationSetting.objects.filter(key__startswith="FEATURE_")}
    flags = [
        {
            "key": key,
            "label": label,
            "description": desc,
            "enabled": db_values.get(key, "false").lower() == "true",
        }
        for key, label, desc in KNOWN_FLAGS
    ]

    return render(request, "admin_tools/feature_flags.html", {"flags": flags})

