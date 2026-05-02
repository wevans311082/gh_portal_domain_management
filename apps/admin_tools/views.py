import json
import time
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask
from django_celery_results.models import TaskResult

from apps.core.runtime_settings import get_runtime_setting
from apps.accounts.models import User
from apps.audit.models import AuditLog, EmailLog
from apps.billing.models import Invoice
from apps.domains.models import Domain, DomainPricingSettings, DomainRenewal, TLDPricing
from apps.payments.models import Payment
from apps.services.models import Service
from apps.support.models import SupportTicket


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
    context.update(_build_task_summary())
    return render(request, "admin_tools/dashboard.html", context)


@staff_member_required
def task_management(request):
    context = _build_task_summary()
    context["periodic_tasks"] = PeriodicTask.objects.select_related("interval", "crontab").order_by(
        "name"
    )[:25]
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

    return render(request, "admin_tools/integrations.html", {"probes": probes})


def _safe_json(obj):
    """Serialise *obj* to a pretty-printed JSON string for display."""
    try:
        return json.dumps(obj, indent=2, default=str)
    except Exception:
        return str(obj)


@staff_member_required
def integration_detail(request, service):
    """Detailed test view for a single integration."""
    from apps.domains.resellerclub_client import ResellerClubClient
    from apps.cloudflare_integration.services import CloudflareService
    from apps.companies.services import CompaniesHouseService
    from apps.provisioning.whm_client import WHMClient
    import stripe as stripe_module
    import requests as _req

    SERVICE_TESTS = {
        "resellerclub": [
            ("Check availability (google.com)", lambda: ResellerClubClient().check_availability(["google"], ["com"])),
            ("Check availability (example.com)", lambda: ResellerClubClient().check_availability(["example"], ["com"])),
            ("Get .com TLD pricing", lambda: ResellerClubClient().get_tld_pricing("com", years=1, action="registration")),
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

    # Set Stripe API key before probe
    stripe_module.api_key = get_runtime_setting("STRIPE_SECRET_KEY", "")

    tests = []
    for label, fn in SERVICE_TESTS[service]:
        probe = _probe(label, fn)
        probe["json"] = _safe_json(probe["data"])
        tests.append(probe)

    SERVICE_LABELS = {
        "resellerclub": "ResellerClub",
        "cloudflare": "Cloudflare",
        "companies-house": "Companies House",
        "whm": "WHM / cPanel",
        "stripe": "Stripe",
    }

    return render(request, "admin_tools/integration_detail.html", {
        "service": service,
        "service_label": SERVICE_LABELS[service],
        "tests": tests,
    })


# ---------------------------------------------------------------------------
# Users overview
# ---------------------------------------------------------------------------

@staff_member_required
def users(request):
    """Paginated user list with search."""
    q = request.GET.get("q", "").strip()
    qs = User.objects.order_by("-created_at")
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

        if action == "sync_all":
            from apps.domains.tasks import sync_tld_pricing

            sync_tld_pricing.delay(tlds=list(settings_obj.supported_tlds or []))
            messages.success(request, "Queued pricing sync for supported TLDs.")
            return redirect(redirect_url)

        if action == "sync_tld":
            from apps.domains.tasks import sync_tld_pricing

            tld = (request.POST.get("tld") or "").strip().lower()
            if not tld:
                messages.error(request, "No TLD selected for sync.")
                return redirect(redirect_url)
            sync_tld_pricing.delay(tlds=[tld])
            messages.success(request, f"Queued pricing sync for .{tld}.")
            return redirect(redirect_url)

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

    return render(request, "admin_tools/settings_overview.html", {"cfg": cfg})


# ---------------------------------------------------------------------------
# Setup wizard alias (redirect to wizard_index)
# ---------------------------------------------------------------------------

@staff_member_required
def setup(request):
    return redirect(reverse("admin_tools:wizard_index"))

