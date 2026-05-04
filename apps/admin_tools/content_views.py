from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django import forms
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.mfa import active_backup_code_count, regenerate_backup_codes
from apps.admin_tools.forms import (
    AdminUserCreateForm,
    AdminUserUpdateForm,
    AnnouncementBannerForm,
    BlogPostForm,
    ErrorPageContentForm,
    HomeFAQForm,
    HomeServiceCardForm,
    LegalPageForm,
    NotificationTemplateForm,
    PackageCardForm,
    PromoCodeForm,
    SiteContentSettingsForm,
    TestimonialForm,
)
from apps.core.models import BlogPost, ErrorPageContent, HomeFAQ, HomeServiceCard, LegalPage, SiteContentSettings, Testimonial, PromoCode, AnnouncementBanner
from apps.notifications.models import NotificationTemplate
from apps.products.models import Package, PackageFeature
from apps.companies.services import CompaniesHouseService


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
def company_lookup(request):
    company_number = (request.GET.get("company_number") or "").strip().replace(" ", "").upper()
    if not company_number:
        return JsonResponse({"ok": False, "error": "Please provide a company number."}, status=400)

    payload = CompaniesHouseService().get_company(company_number)
    if not payload:
        return JsonResponse(
            {
                "ok": False,
                "error": "Company not found or Companies House API is not configured.",
            },
            status=404,
        )

    office = payload.get("registered_office_address") or {}
    address_parts = [
        office.get("address_line_1", ""),
        office.get("address_line_2", ""),
        office.get("locality", ""),
        office.get("region", ""),
        office.get("postal_code", ""),
        office.get("country", ""),
    ]

    return JsonResponse(
        {
            "ok": True,
            "company_number": payload.get("company_number") or company_number,
            "company_name": payload.get("company_name") or "",
            "company_status": payload.get("company_status") or "",
            "company_type": payload.get("type") or "",
            "address": ", ".join([part for part in address_parts if part]),
        }
    )


@staff_member_required
def user_mfa_manage(request, pk):
    user = get_object_or_404(User, pk=pk)
    generated_codes = None

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "disable_mfa":
            user.mfa_enabled = False
            user.mfa_secret = ""
            user.save(update_fields=["mfa_enabled", "mfa_secret"])
            user.mfa_backup_codes.all().delete()
            messages.success(request, f"MFA disabled for {user.email}.")
            return redirect("admin_tools:user_mfa_manage", pk=user.pk)

        if action == "enable_mfa_pending_setup":
            user.mfa_enabled = False
            user.mfa_secret = ""
            user.save(update_fields=["mfa_enabled", "mfa_secret"])
            user.mfa_backup_codes.all().delete()
            messages.success(request, f"MFA reset for {user.email}. User must reconfigure on next setup.")
            return redirect("admin_tools:user_mfa_manage", pk=user.pk)

        if action == "regenerate_backup_codes":
            if not user.mfa_enabled:
                messages.error(request, "User must have MFA enabled before backup codes can be generated.")
            else:
                generated_codes = regenerate_backup_codes(user)
                messages.success(request, f"New backup codes generated for {user.email}.")

    return render(
        request,
        "admin_tools/content/user_mfa_manage.html",
        {
            "obj": user,
            "backup_code_count": active_backup_code_count(user),
            "generated_codes": generated_codes,
        },
    )


@staff_member_required
@require_POST
def user_su_start(request, pk):
    target = get_object_or_404(User, pk=pk)
    actor = request.user

    if actor.pk == target.pk:
        messages.info(request, "You are already this user.")
        return redirect("portal:dashboard")

    if request.session.get("impersonator_user_id"):
        messages.error(request, "Finish the current impersonation session before starting another.")
        return redirect("admin_tools:users")

    try:
        from apps.audit.models import AuditLog

        AuditLog.objects.create(
            user=actor,
            action="impersonation.started",
            model_name="accounts.User",
            object_id=str(target.pk),
            ip_address=request.META.get("REMOTE_ADDR") or None,
            data={"target_email": target.email},
        )
    except Exception:
        pass

    login(request, target, backend="apps.accounts.backends.EmailBackend")
    request.session["impersonator_user_id"] = actor.pk
    request.session["impersonator_started_at"] = timezone.now().isoformat()
    request.session.modified = True
    messages.warning(request, f"Impersonating {target.email}. Actions are audited.")
    return redirect("portal:dashboard")


@login_required
@require_POST
def user_su_stop(request):
    impersonator_id = request.session.get("impersonator_user_id")
    if not impersonator_id:
        messages.info(request, "No active impersonation session.")
        return redirect("admin_tools:dashboard")

    impersonator = get_object_or_404(User, pk=impersonator_id, is_staff=True)
    request.session.pop("impersonator_user_id", None)
    request.session.pop("impersonator_started_at", None)
    request.session.modified = True

    try:
        from apps.audit.models import AuditLog

        AuditLog.objects.create(
            user=impersonator,
            action="impersonation.stopped",
            model_name="accounts.User",
            object_id=str(request.user.pk),
            ip_address=request.META.get("REMOTE_ADDR") or None,
            data={"acted_as_email": request.user.email},
        )
    except Exception:
        pass

    login(request, impersonator, backend="apps.accounts.backends.EmailBackend")
    messages.success(request, f"Returned to staff account {impersonator.email}.")
    return redirect("admin_tools:users")


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


# ---------------------------------------------------------------------------
# Phase 5 CMS: Blog posts
# ---------------------------------------------------------------------------

@staff_member_required
def blog_post_list(request):
    posts = BlogPost.objects.order_by("-created_at")
    return render(request, "admin_tools/content/blog_post_list.html", {"posts": posts})


@staff_member_required
def blog_post_create(request):
    if request.method == "POST":
        form = BlogPostForm(request.POST)
        if form.is_valid():
            post = form.save(commit=False)
            if not post.author_id:
                post.author = request.user
            post.save()
            messages.success(request, f'Blog post "{post.title}" created.')
            return redirect("admin_tools:blog_post_list")
    else:
        form = BlogPostForm(initial={"author": request.user})
    return render(request, "admin_tools/content/blog_post_form.html", {"form": form, "mode": "create"})


@staff_member_required
def blog_post_edit(request, pk):
    post = get_object_or_404(BlogPost, pk=pk)
    if request.method == "POST":
        form = BlogPostForm(request.POST, instance=post)
        if form.is_valid():
            form.save()
            messages.success(request, f'Blog post "{post.title}" saved.')
            return redirect("admin_tools:blog_post_list")
    else:
        form = BlogPostForm(instance=post)
    return render(request, "admin_tools/content/blog_post_form.html", {"form": form, "mode": "edit", "obj": post})


@staff_member_required
@require_POST
def blog_post_delete(request, pk):
    post = get_object_or_404(BlogPost, pk=pk)
    title = post.title
    post.delete()
    messages.success(request, f'Blog post "{title}" deleted.')
    return redirect("admin_tools:blog_post_list")


# ---------------------------------------------------------------------------
# Phase 5 CMS: Testimonials
# ---------------------------------------------------------------------------

@staff_member_required
def testimonial_list(request):
    testimonials = Testimonial.objects.order_by("sort_order", "-created_at")
    return render(request, "admin_tools/content/testimonial_list.html", {"testimonials": testimonials})


@staff_member_required
def testimonial_create(request):
    if request.method == "POST":
        form = TestimonialForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Testimonial created.")
            return redirect("admin_tools:testimonial_list")
    else:
        form = TestimonialForm()
    return render(request, "admin_tools/content/testimonial_form.html", {"form": form, "mode": "create"})


@staff_member_required
def testimonial_edit(request, pk):
    testimonial = get_object_or_404(Testimonial, pk=pk)
    if request.method == "POST":
        form = TestimonialForm(request.POST, instance=testimonial)
        if form.is_valid():
            form.save()
            messages.success(request, "Testimonial saved.")
            return redirect("admin_tools:testimonial_list")
    else:
        form = TestimonialForm(instance=testimonial)
    return render(request, "admin_tools/content/testimonial_form.html", {"form": form, "mode": "edit", "obj": testimonial})


@staff_member_required
@require_POST
def testimonial_delete(request, pk):
    testimonial = get_object_or_404(Testimonial, pk=pk)
    testimonial.delete()
    messages.success(request, "Testimonial deleted.")
    return redirect("admin_tools:testimonial_list")


# ---------------------------------------------------------------------------
# Email template CRUD
# ---------------------------------------------------------------------------

@staff_member_required
def email_template_list(request):
    from apps.notifications.services import NOTIFICATION_TEMPLATES
    db_templates = {t.name: t for t in NotificationTemplate.objects.all()}
    templates = []
    for name in sorted(NOTIFICATION_TEMPLATES.keys()):
        db_tpl = db_templates.get(name)
        templates.append({
            "name": name,
            "db_template": db_tpl,
            "overridden": db_tpl is not None and db_tpl.is_active,
        })
    # Include any custom DB-only templates not in NOTIFICATION_TEMPLATES
    for name, db_tpl in db_templates.items():
        if name not in NOTIFICATION_TEMPLATES:
            templates.append({"name": name, "db_template": db_tpl, "overridden": True})
    return render(request, "admin_tools/content/email_template_list.html", {"templates": templates})


@staff_member_required
def email_template_edit(request, name):
    from apps.notifications.services import NOTIFICATION_TEMPLATES
    tpl = NotificationTemplate.objects.filter(name=name).first()
    builtin = NOTIFICATION_TEMPLATES.get(name, {})
    if request.method == "POST":
        form = NotificationTemplateForm(request.POST, instance=tpl)
        if form.is_valid():
            form.save()
            messages.success(request, f"Email template '{name}' saved.")
            return redirect("admin_tools:email_template_list")
    else:
        if tpl is None:
            initial = {
                "name": name,
                "subject": builtin.get("subject", ""),
                "html_content": "",
                "text_content": "",
                "is_active": True,
            }
            form = NotificationTemplateForm(initial=initial)
        else:
            form = NotificationTemplateForm(instance=tpl)
    return render(request, "admin_tools/content/email_template_form.html", {
        "form": form,
        "template_name": name,
        "builtin": builtin,
    })


@staff_member_required
@require_POST
def email_template_delete(request, name):
    tpl = get_object_or_404(NotificationTemplate, name=name)
    tpl.delete()
    messages.success(request, f"Email template override for '{name}' removed (builtin restored).")
    return redirect("admin_tools:email_template_list")


# ---------------------------------------------------------------------------
# Phase 7: Promo codes
# ---------------------------------------------------------------------------

@staff_member_required
def promo_code_list(request):
    codes = PromoCode.objects.all()
    return render(request, "admin_tools/content/promo_code_list.html", {"codes": codes})


@staff_member_required
def promo_code_create(request):
    if request.method == "POST":
        form = PromoCodeForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Promo code created.")
            return redirect("admin_tools:promo_code_list")
    else:
        form = PromoCodeForm()
    return render(request, "admin_tools/content/promo_code_form.html", {"form": form, "mode": "create"})


@staff_member_required
def promo_code_edit(request, pk):
    code = get_object_or_404(PromoCode, pk=pk)
    if request.method == "POST":
        form = PromoCodeForm(request.POST, instance=code)
        if form.is_valid():
            form.save()
            messages.success(request, "Promo code updated.")
            return redirect("admin_tools:promo_code_list")
    else:
        form = PromoCodeForm(instance=code)
    return render(request, "admin_tools/content/promo_code_form.html", {"form": form, "mode": "edit", "object": code})


@staff_member_required
@require_POST
def promo_code_delete(request, pk):
    code = get_object_or_404(PromoCode, pk=pk)
    code.delete()
    messages.success(request, "Promo code deleted.")
    return redirect("admin_tools:promo_code_list")


# ---------------------------------------------------------------------------
# Phase 7: Announcement banners
# ---------------------------------------------------------------------------

@staff_member_required
def banner_list(request):
    banners = AnnouncementBanner.objects.all()
    return render(request, "admin_tools/content/banner_list.html", {"banners": banners})


@staff_member_required
def banner_create(request):
    if request.method == "POST":
        form = AnnouncementBannerForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Banner created.")
            return redirect("admin_tools:banner_list")
    else:
        form = AnnouncementBannerForm()
    return render(request, "admin_tools/content/banner_form.html", {"form": form, "mode": "create"})


@staff_member_required
def banner_edit(request, pk):
    banner = get_object_or_404(AnnouncementBanner, pk=pk)
    if request.method == "POST":
        form = AnnouncementBannerForm(request.POST, instance=banner)
        if form.is_valid():
            form.save()
            messages.success(request, "Banner updated.")
            return redirect("admin_tools:banner_list")
    else:
        form = AnnouncementBannerForm(instance=banner)
    return render(request, "admin_tools/content/banner_form.html", {"form": form, "mode": "edit", "object": banner})


@staff_member_required
@require_POST
def banner_delete(request, pk):
    banner = get_object_or_404(AnnouncementBanner, pk=pk)
    banner.delete()
    messages.success(request, "Banner deleted.")
    return redirect("admin_tools:banner_list")


# ---------------------------------------------------------------------------
# Phase 8: IP allowlist management
# ---------------------------------------------------------------------------

@staff_member_required
def ip_allowlist(request):
    from apps.audit.models import IPAllowlistEntry
    entries = IPAllowlistEntry.objects.all()
    return render(request, "admin_tools/security/ip_allowlist.html", {"entries": entries})


@staff_member_required
def ip_allowlist_create(request):
    from apps.audit.models import IPAllowlistEntry

    class _Form(forms.ModelForm):
        class Meta:
            model = IPAllowlistEntry
            fields = ["ip_address", "label", "is_active"]

    form = _Form(request.POST or None)
    if request.method == "POST" and form.is_valid():
        entry = form.save(commit=False)
        entry.added_by = request.user
        entry.save()
        messages.success(request, f"IP {entry.ip_address} added to allowlist.")
        return redirect("admin_tools:ip_allowlist")
    return render(request, "admin_tools/security/ip_allowlist_form.html", {"form": form, "mode": "create"})


@staff_member_required
@require_POST
def ip_allowlist_delete(request, pk):
    from apps.audit.models import IPAllowlistEntry
    entry = get_object_or_404(IPAllowlistEntry, pk=pk)
    ip = entry.ip_address
    entry.delete()
    messages.success(request, f"IP {ip} removed from allowlist.")
    return redirect("admin_tools:ip_allowlist")
