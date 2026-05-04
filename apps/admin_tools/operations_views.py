from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.admin_tools.forms import (
    DomainForm,
    PaymentForm,
    ServiceForm,
    SupportTicketForm,
    WebsiteTemplateForm,
)
from apps.domains.models import Domain
from apps.payments.models import Payment
from apps.services.models import Service
from apps.support.models import SupportTicket
from apps.website_templates.models import WebsiteTemplate


@staff_member_required
def domains_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = Domain.objects.select_related("user", "order__registration_contact").order_by("name")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(user__email__icontains=q) | Q(status__icontains=q))
    page_obj = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "admin_tools/ops/domains_list.html", {"page_obj": page_obj, "search_q": q})


@staff_member_required
def domains_create(request):
    if request.method == "POST":
        form = DomainForm(request.POST)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Domain {obj.name} created.")
            return redirect("admin_tools:domains_edit", pk=obj.pk)
    else:
        form = DomainForm()
    return render(request, "admin_tools/ops/domain_form.html", {"form": form, "mode": "create"})


@staff_member_required
def domains_edit(request, pk):
    obj = get_object_or_404(Domain, pk=pk)
    if request.method == "POST":
        form = DomainForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, f"Domain {obj.name} updated.")
            return redirect("admin_tools:domains_edit", pk=obj.pk)
    else:
        form = DomainForm(instance=obj)
    return render(request, "admin_tools/ops/domain_form.html", {"form": form, "mode": "edit", "obj": obj})


@staff_member_required
@require_POST
def domains_delete(request, pk):
    obj = get_object_or_404(Domain, pk=pk)
    name = obj.name
    obj.delete()
    messages.success(request, f"Domain {name} deleted.")
    return redirect("admin_tools:domains_list")


@staff_member_required
def services_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = Service.objects.select_related("user", "package").order_by("-created_at")
    if q:
        qs = qs.filter(
            Q(domain_name__icontains=q)
            | Q(user__email__icontains=q)
            | Q(package__name__icontains=q)
            | Q(status__icontains=q)
        )
    page_obj = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "admin_tools/ops/services_list.html", {"page_obj": page_obj, "search_q": q})


@staff_member_required
def services_create(request):
    if request.method == "POST":
        form = ServiceForm(request.POST)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Service #{obj.pk} created.")
            return redirect("admin_tools:services_edit", pk=obj.pk)
    else:
        form = ServiceForm()
    return render(request, "admin_tools/ops/service_form.html", {"form": form, "mode": "create"})


@staff_member_required
def services_edit(request, pk):
    obj = get_object_or_404(Service, pk=pk)
    if request.method == "POST":
        form = ServiceForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, f"Service #{obj.pk} updated.")
            return redirect("admin_tools:services_edit", pk=obj.pk)
    else:
        form = ServiceForm(instance=obj)
    return render(request, "admin_tools/ops/service_form.html", {"form": form, "mode": "edit", "obj": obj})


@staff_member_required
@require_POST
def services_delete(request, pk):
    obj = get_object_or_404(Service, pk=pk)
    oid = obj.pk
    obj.delete()
    messages.success(request, f"Service #{oid} deleted.")
    return redirect("admin_tools:services_list")


@staff_member_required
def tickets_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = SupportTicket.objects.select_related("user", "department", "assigned_to").order_by("-created_at")
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(user__email__icontains=q) | Q(status__icontains=q))
    page_obj = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "admin_tools/ops/tickets_list.html", {"page_obj": page_obj, "search_q": q})


@staff_member_required
def tickets_create(request):
    if request.method == "POST":
        form = SupportTicketForm(request.POST)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Ticket #{obj.pk} created.")
            return redirect("admin_tools:tickets_edit", pk=obj.pk)
    else:
        form = SupportTicketForm()
    return render(request, "admin_tools/ops/ticket_form.html", {"form": form, "mode": "create"})


@staff_member_required
def tickets_edit(request, pk):
    obj = get_object_or_404(SupportTicket, pk=pk)
    if request.method == "POST":
        form = SupportTicketForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, f"Ticket #{obj.pk} updated.")
            return redirect("admin_tools:tickets_edit", pk=obj.pk)
    else:
        form = SupportTicketForm(instance=obj)
    return render(request, "admin_tools/ops/ticket_form.html", {"form": form, "mode": "edit", "obj": obj})


@staff_member_required
@require_POST
def tickets_delete(request, pk):
    obj = get_object_or_404(SupportTicket, pk=pk)
    oid = obj.pk
    obj.delete()
    messages.success(request, f"Ticket #{oid} deleted.")
    return redirect("admin_tools:tickets_list")


@staff_member_required
def payments_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = Payment.objects.select_related("user", "invoice").order_by("-created_at")
    if q:
        qs = qs.filter(
            Q(user__email__icontains=q)
            | Q(external_id__icontains=q)
            | Q(provider__icontains=q)
            | Q(status__icontains=q)
        )
    page_obj = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "admin_tools/ops/payments_list.html", {"page_obj": page_obj, "search_q": q})


@staff_member_required
def payments_create(request):
    if request.method == "POST":
        form = PaymentForm(request.POST)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Payment #{obj.pk} created.")
            return redirect("admin_tools:payments_edit", pk=obj.pk)
    else:
        form = PaymentForm()
    return render(request, "admin_tools/ops/payment_form.html", {"form": form, "mode": "create"})


@staff_member_required
def payments_edit(request, pk):
    obj = get_object_or_404(Payment, pk=pk)
    if request.method == "POST":
        form = PaymentForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, f"Payment #{obj.pk} updated.")
            return redirect("admin_tools:payments_edit", pk=obj.pk)
    else:
        form = PaymentForm(instance=obj)
    return render(request, "admin_tools/ops/payment_form.html", {"form": form, "mode": "edit", "obj": obj})


@staff_member_required
@require_POST
def payments_delete(request, pk):
    obj = get_object_or_404(Payment, pk=pk)
    oid = obj.pk
    obj.delete()
    messages.success(request, f"Payment #{oid} deleted.")
    return redirect("admin_tools:payments_list")


@staff_member_required
def templates_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = WebsiteTemplate.objects.order_by("category", "name")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(slug__icontains=q) | Q(category__icontains=q))
    page_obj = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "admin_tools/ops/templates_list.html", {"page_obj": page_obj, "search_q": q})


@staff_member_required
def templates_create(request):
    if request.method == "POST":
        form = WebsiteTemplateForm(request.POST)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Template {obj.name} created.")
            return redirect("admin_tools:templates_edit", pk=obj.pk)
    else:
        form = WebsiteTemplateForm()
    return render(request, "admin_tools/ops/template_form.html", {"form": form, "mode": "create"})


@staff_member_required
def templates_edit(request, pk):
    obj = get_object_or_404(WebsiteTemplate, pk=pk)
    if request.method == "POST":
        form = WebsiteTemplateForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, f"Template {obj.name} updated.")
            return redirect("admin_tools:templates_edit", pk=obj.pk)
    else:
        form = WebsiteTemplateForm(instance=obj)
    return render(request, "admin_tools/ops/template_form.html", {"form": form, "mode": "edit", "obj": obj})


@staff_member_required
@require_POST
def templates_delete(request, pk):
    obj = get_object_or_404(WebsiteTemplate, pk=pk)
    name = obj.name
    obj.delete()
    messages.success(request, f"Template {name} deleted.")
    return redirect("admin_tools:templates_list")
