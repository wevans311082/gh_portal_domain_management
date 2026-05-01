from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask
from django_celery_results.models import TaskResult

from apps.accounts.models import User
from apps.billing.models import Invoice
from apps.domains.models import Domain, DomainRenewal
from apps.services.models import Service


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
    context = {
        "total_users": User.objects.count(),
        "active_services": Service.objects.filter(status="active").count(),
        "unpaid_invoices": Invoice.objects.filter(status="unpaid").count(),
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
