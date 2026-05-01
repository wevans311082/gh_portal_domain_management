from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask
from django_celery_results.models import TaskResult

from apps.accounts.models import User
from apps.billing.models import Invoice
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
