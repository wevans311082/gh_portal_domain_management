from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.admin_tools.forms import (
    AdminUserCreateForm,
    AdminUserUpdateForm,
    ErrorPageContentForm,
    HomeFAQForm,
    HomeServiceCardForm,
    LegalPageForm,
    PackageCardForm,
    SiteContentSettingsForm,
)
from apps.core.models import ErrorPageContent, HomeFAQ, HomeServiceCard, LegalPage, SiteContentSettings
from apps.products.models import Package, PackageFeature


@staff_member_required
def user_create(request):
    if request.method == "POST":
        form = AdminUserCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, f"User {user.email} created.")
            return redirect("admin_tools:user_edit", pk=user.pk)
    else:
        form = AdminUserCreateForm()
    return render(request, "admin_tools/content/user_form.html", {"form": form, "mode": "create"})


@staff_member_required
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        form = AdminUserUpdateForm(request.POST, instance=user)
        if form.is_valid():
            form.save()
            messages.success(request, f"User {user.email} saved.")
            return redirect("admin_tools:user_edit", pk=user.pk)
    else:
        form = AdminUserUpdateForm(instance=user)
    return render(request, "admin_tools/content/user_form.html", {"form": form, "mode": "edit", "obj": user})


@staff_member_required
def content_dashboard(request):
    return render(
        request,
        "admin_tools/content/dashboard.html",
        {
            "faq_count": HomeFAQ.objects.count(),
            "service_count": HomeServiceCard.objects.count(),
            "package_count": Package.objects.count(),
            "legal_count": LegalPage.objects.count(),
            "error_page_count": ErrorPageContent.objects.count(),
        },
    )


@staff_member_required
def content_settings_edit(request):
    settings_obj = SiteContentSettings.get_solo()
    if request.method == "POST":
        form = SiteContentSettingsForm(request.POST, instance=settings_obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Site content settings saved.")
            return redirect("admin_tools:content_settings")
    else:
        form = SiteContentSettingsForm(instance=settings_obj)
    return render(
        request,
        "admin_tools/content/site_settings_form.html",
        {
            "form": form,
            "settings_obj": settings_obj,
            "legal_links": LegalPage.objects.filter(is_published=True, show_in_footer=True).order_by("sort_order", "title"),
        },
    )


@staff_member_required
def faq_list(request):
    faqs = HomeFAQ.objects.order_by("sort_order", "id")
    return render(request, "admin_tools/content/faq_list.html", {"faqs": faqs})


@staff_member_required
def faq_create(request):
    if request.method == "POST":
        form = HomeFAQForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "FAQ created.")
            return redirect("admin_tools:faq_list")
    else:
        form = HomeFAQForm()
    return render(request, "admin_tools/content/faq_form.html", {"form": form, "mode": "create"})


@staff_member_required
def faq_edit(request, pk):
    faq = get_object_or_404(HomeFAQ, pk=pk)
    if request.method == "POST":
        form = HomeFAQForm(request.POST, instance=faq)
        if form.is_valid():
            form.save()
            messages.success(request, "FAQ saved.")
            return redirect("admin_tools:faq_list")
    else:
        form = HomeFAQForm(instance=faq)
    return render(request, "admin_tools/content/faq_form.html", {"form": form, "mode": "edit", "obj": faq})


@staff_member_required
@require_POST
def faq_delete(request, pk):
    faq = get_object_or_404(HomeFAQ, pk=pk)
    faq.delete()
    messages.success(request, "FAQ deleted.")
    return redirect("admin_tools:faq_list")


@staff_member_required
def service_card_list(request):
    cards = HomeServiceCard.objects.order_by("sort_order", "id")
    return render(request, "admin_tools/content/service_card_list.html", {"cards": cards})


@staff_member_required
def service_card_create(request):
    if request.method == "POST":
        form = HomeServiceCardForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Service card created.")
            return redirect("admin_tools:service_card_list")
    else:
        form = HomeServiceCardForm()
    return render(request, "admin_tools/content/service_card_form.html", {"form": form, "mode": "create"})


@staff_member_required
def service_card_edit(request, pk):
    card = get_object_or_404(HomeServiceCard, pk=pk)
    if request.method == "POST":
        form = HomeServiceCardForm(request.POST, instance=card)
        if form.is_valid():
            form.save()
            messages.success(request, "Service card saved.")
            return redirect("admin_tools:service_card_list")
    else:
        form = HomeServiceCardForm(instance=card)
    return render(request, "admin_tools/content/service_card_form.html", {"form": form, "mode": "edit", "obj": card})


@staff_member_required
@require_POST
def service_card_delete(request, pk):
    card = get_object_or_404(HomeServiceCard, pk=pk)
    card.delete()
    messages.success(request, "Service card deleted.")
    return redirect("admin_tools:service_card_list")


@staff_member_required
def package_card_list(request):
    packages = Package.objects.prefetch_related("features").order_by("card_sort_order", "sort_order", "price_monthly")
    return render(request, "admin_tools/content/package_card_list.html", {"packages": packages})


@staff_member_required
def package_card_edit(request, pk):
    package = get_object_or_404(Package, pk=pk)
    if request.method == "POST":
        form = PackageCardForm(request.POST, instance=package)
        if form.is_valid():
            package = form.save()

            # Replace package features from textarea lines (supports +/- prefix)
            raw_lines = request.POST.get("feature_lines", "")
            PackageFeature.objects.filter(package=package).delete()
            order = 0
            for raw in (line.strip() for line in raw_lines.splitlines()):
                if not raw:
                    continue
                is_positive = True
                text = raw
                if raw.startswith("-"):
                    is_positive = False
                    text = raw[1:].strip()
                elif raw.startswith("+"):
                    text = raw[1:].strip()
                if not text:
                    continue
                PackageFeature.objects.create(
                    package=package,
                    text=text,
                    is_positive=is_positive,
                    is_active=True,
                    sort_order=order,
                )
                order += 1

            messages.success(request, "Package card saved.")
            return redirect("admin_tools:package_card_edit", pk=package.pk)
    else:
        form = PackageCardForm(instance=package)

    feature_lines = "\n".join(
        [
            f"{'+' if f.is_positive else '-'} {f.text}"
            for f in package.features.filter(is_active=True).order_by("sort_order", "id")
        ]
    )
    return render(
        request,
        "admin_tools/content/package_card_form.html",
        {
            "form": form,
            "obj": package,
            "feature_lines": feature_lines,
            "preview_features": package.features.filter(is_active=True).order_by("sort_order", "id"),
        },
    )


@staff_member_required
def legal_page_list(request):
    pages = LegalPage.objects.order_by("sort_order", "title")
    return render(request, "admin_tools/content/legal_page_list.html", {"pages": pages})


@staff_member_required
def legal_page_create(request):
    if request.method == "POST":
        form = LegalPageForm(request.POST)
        if form.is_valid():
            page = form.save()
            messages.success(request, "Legal page created.")
            return redirect("admin_tools:legal_page_edit", pk=page.pk)
    else:
        form = LegalPageForm()
    return render(request, "admin_tools/content/legal_page_form.html", {"form": form, "mode": "create"})


@staff_member_required
def legal_page_edit(request, pk):
    page = get_object_or_404(LegalPage, pk=pk)
    if request.method == "POST":
        form = LegalPageForm(request.POST, instance=page)
        if form.is_valid():
            page = form.save()
            messages.success(request, "Legal page saved.")
            return redirect("admin_tools:legal_page_edit", pk=page.pk)
    else:
        form = LegalPageForm(instance=page)
    return render(request, "admin_tools/content/legal_page_form.html", {"form": form, "mode": "edit", "obj": page})


@staff_member_required
@require_POST
def legal_page_delete(request, pk):
    page = get_object_or_404(LegalPage, pk=pk)
    page.delete()
    messages.success(request, "Legal page deleted.")
    return redirect("admin_tools:legal_page_list")


@staff_member_required
def error_page_list(request):
    pages = ErrorPageContent.objects.order_by("status_code")
    return render(request, "admin_tools/content/error_page_list.html", {"pages": pages})


@staff_member_required
def error_page_create(request):
    if request.method == "POST":
        form = ErrorPageContentForm(request.POST)
        if form.is_valid():
            page = form.save()
            messages.success(request, "Error page content created.")
            return redirect("admin_tools:error_page_edit", pk=page.pk)
    else:
        form = ErrorPageContentForm()
    return render(request, "admin_tools/content/error_page_form.html", {"form": form, "mode": "create"})


@staff_member_required
def error_page_edit(request, pk):
    page = get_object_or_404(ErrorPageContent, pk=pk)
    if request.method == "POST":
        form = ErrorPageContentForm(request.POST, instance=page)
        if form.is_valid():
            page = form.save()
            messages.success(request, "Error page content saved.")
            return redirect("admin_tools:error_page_edit", pk=page.pk)
    else:
        form = ErrorPageContentForm(instance=page)
    return render(request, "admin_tools/content/error_page_form.html", {"form": form, "mode": "edit", "obj": page})


@staff_member_required
@require_POST
def error_page_delete(request, pk):
    page = get_object_or_404(ErrorPageContent, pk=pk)
    page.delete()
    messages.success(request, "Error page content deleted.")
    return redirect("admin_tools:error_page_list")
