import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.provisioning.forms import DatabaseForm, EmailAccountForm
from apps.provisioning.models import ProvisioningJob
from apps.provisioning.tasks import (
    create_database_task,
    create_email_account_task,
    delete_email_account_task,
)
from apps.provisioning.whm_client import WHMClient, WHMClientError
from apps.services.models import Service

logger = logging.getLogger(__name__)


def _get_active_service(request, service_id: int) -> Service:
    """Return a Service owned by the request user, or 404."""
    return get_object_or_404(Service, id=service_id, user=request.user)


# ── Service list ──────────────────────────────────────────────────────────────

@login_required
def service_list(request):
    """Customer view: list all hosting services."""
    services = request.user.services.select_related("package").order_by("-created_at")
    return render(request, "provisioning/service_list.html", {"services": services})


# ── Service detail (email + db + quota) ──────────────────────────────────────

@login_required
def service_detail(request, service_id: int):
    """Customer view: dashboard for a single hosting service."""
    service = _get_active_service(request, service_id)

    email_accounts = []
    databases = []
    quota = {}

    if service.cpanel_username:
        try:
            client = WHMClient()
            email_accounts = client.list_email_accounts(service.cpanel_username)
            databases = client.list_databases(service.cpanel_username)
            quota = client.get_quota(service.cpanel_username)
        except WHMClientError as e:
            logger.error(f"WHM error fetching data for service {service_id}: {e}")
            messages.error(request, "Could not fetch live cPanel data. Please try again later.")

    return render(request, "provisioning/service_detail.html", {
        "service": service,
        "email_accounts": email_accounts,
        "databases": databases,
        "quota": quota,
        "email_form": EmailAccountForm(),
        "db_form": DatabaseForm(),
    })


# ── Email account actions ─────────────────────────────────────────────────────

@login_required
def email_create(request, service_id: int):
    """Create a new email account via a Celery task."""
    service = _get_active_service(request, service_id)

    if not service.cpanel_username:
        messages.error(request, "Your hosting account is not yet fully provisioned.")
        return redirect("provisioning:service_detail", service_id=service_id)

    if request.method == "POST":
        form = EmailAccountForm(request.POST)
        if form.is_valid():
            create_email_account_task.delay(
                service_id=service.id,
                email_user=form.cleaned_data["email_user"],
                domain=service.domain_name,
                password=form.cleaned_data["password"],
                quota_mb=form.cleaned_data["quota_mb"],
            )
            messages.success(
                request,
                f"Email account {form.cleaned_data['email_user']}@{service.domain_name} "
                "is being created. Refresh in a moment.",
            )
            return redirect("provisioning:service_detail", service_id=service_id)
    else:
        form = EmailAccountForm()

    return render(request, "provisioning/email_form.html", {
        "form": form,
        "service": service,
    })


@login_required
@require_POST
def email_delete(request, service_id: int):
    """Queue deletion of an email account."""
    service = _get_active_service(request, service_id)

    if not service.cpanel_username:
        messages.error(request, "Your hosting account is not yet fully provisioned.")
        return redirect("provisioning:service_detail", service_id=service_id)

    email_user = request.POST.get("email_user", "").strip()
    domain = request.POST.get("domain", "").strip()

    if not email_user or not domain:
        messages.error(request, "Invalid request.")
        return redirect("provisioning:service_detail", service_id=service_id)

    delete_email_account_task.delay(
        service_id=service.id,
        email_user=email_user,
        domain=domain,
    )
    messages.success(request, f"Email account {email_user}@{domain} is being deleted.")
    return redirect("provisioning:service_detail", service_id=service_id)


# ── Database actions ──────────────────────────────────────────────────────────

@login_required
def database_create(request, service_id: int):
    """Create a new MySQL database via a Celery task."""
    service = _get_active_service(request, service_id)

    if not service.cpanel_username:
        messages.error(request, "Your hosting account is not yet fully provisioned.")
        return redirect("provisioning:service_detail", service_id=service_id)

    if request.method == "POST":
        form = DatabaseForm(request.POST)
        if form.is_valid():
            create_database_task.delay(
                service_id=service.id,
                db_name=form.cleaned_data["db_name"],
            )
            full_name = f"{service.cpanel_username}_{form.cleaned_data['db_name']}"
            messages.success(request, f"Database '{full_name}' is being created.")
            return redirect("provisioning:service_detail", service_id=service_id)
    else:
        form = DatabaseForm()

    return render(request, "provisioning/db_form.html", {
        "form": form,
        "service": service,
    })


# ── Provisioning job list (admin-style) ───────────────────────────────────────

@login_required
def job_list(request):
    """Legacy / staff view: list provisioning jobs for this user's services."""
    jobs = ProvisioningJob.objects.filter(
        service__user=request.user,
    ).select_related("service").order_by("-created_at")[:50]
    return render(request, "provisioning/job_list.html", {"jobs": jobs})
