from django.shortcuts import render
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.db import connection
from django.core.cache import cache


def health_check(request):
    status = {"status": "ok", "database": "ok", "cache": "ok"}
    http_status = 200

    try:
        connection.ensure_connection()
    except Exception as e:
        status["database"] = f"error: {str(e)}"
        status["status"] = "degraded"
        http_status = 503

    try:
        cache.set("health_check", "ok", 30)
        cache.get("health_check")
    except Exception as e:
        status["cache"] = f"error: {str(e)}"
        status["status"] = "degraded"
        http_status = 503

    return JsonResponse(status, status=http_status)


def home(request):
    from apps.products.models import Package
    from apps.core.models import HomeFAQ, HomeServiceCard, SiteContentSettings

    packages = (
        Package.objects.filter(is_active=True, show_on_homepage=True)
        .prefetch_related("features")
        .order_by("card_sort_order", "sort_order", "price_monthly")[:6]
    )
    faqs = HomeFAQ.objects.filter(is_active=True).order_by("sort_order", "id")
    service_cards = HomeServiceCard.objects.filter(is_active=True).order_by("sort_order", "id")
    content_settings = SiteContentSettings.get_solo()

    return render(
        request,
        "public/home.html",
        {
            "home_packages": packages,
            "home_faqs": faqs,
            "home_service_cards": service_cards,
            "content_settings": content_settings,
        },
    )


def pricing(request):
    from apps.products.models import Package
    from apps.domains.models import DomainPricingSettings, TLDPricing

    packages = Package.objects.filter(is_active=True).order_by("price_monthly")

    settings = DomainPricingSettings.get_solo()
    fallback_tlds = ["com", "co.uk", "uk", "org", "net", "io"]
    preferred_tlds = (settings.supported_tlds or [])[:6] or fallback_tlds

    pricing_by_tld = {
        p.tld.lower(): p
        for p in TLDPricing.objects.filter(is_active=True)
    }

    popular_tlds = []
    for raw_tld in preferred_tlds:
        key = (raw_tld or "").strip().lower().lstrip(".")
        if not key:
            continue
        row = pricing_by_tld.get(key)
        if not row:
            continue
        popular_tlds.append(
            {
                "label": f".{key}",
                "currency": row.currency,
                "registration_price": row.registration_price,
                "renewal_price": row.renewal_price,
            }
        )

    if not popular_tlds:
        for key in fallback_tlds:
            row = pricing_by_tld.get(key)
            if not row:
                continue
            popular_tlds.append(
                {
                    "label": f".{key}",
                    "currency": row.currency,
                    "registration_price": row.registration_price,
                    "renewal_price": row.renewal_price,
                }
            )

    return render(
        request,
        "public/pricing.html",
        {
            "packages": packages,
            "popular_tlds": popular_tlds,
        },
    )


def contact(request):
    from apps.core.models import ContactFormSettings, ContactSubmission
    from apps.core.forms import ContactForm

    settings = ContactFormSettings.get_solo()

    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            # Honeypot check
            if form.cleaned_data.get("website"):
                # Silent bot reject — show thank-you without saving
                return render(
                    request,
                    "public/contact.html",
                    {"form": ContactForm(), "settings": settings, "submitted": True},
                )

            name = form.cleaned_data["name"]
            email = form.cleaned_data["email"]
            phone = form.cleaned_data.get("phone", "")
            subject = form.cleaned_data.get("subject", "")
            message = form.cleaned_data["message"]

            def _client_ip():
                fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
                return fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", "")

            # Store in DB if destination includes db
            if settings.destination in (ContactFormSettings.DESTINATION_DB, ContactFormSettings.DESTINATION_BOTH):
                ContactSubmission.objects.create(
                    name=name,
                    email=email,
                    phone=phone,
                    subject=subject,
                    message=message,
                    ip_address=_client_ip() or None,
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
                )

            # Send email if destination includes email
            if settings.destination in (ContactFormSettings.DESTINATION_EMAIL, ContactFormSettings.DESTINATION_BOTH):
                dest = settings.destination_email
                if dest:
                    try:
                        from django.core.mail import send_mail
                        from django.conf import settings as django_settings

                        plain_body = (
                            f"You have received a new contact form submission.\n\n"
                            f"Name:    {name}\n"
                            f"Email:   {email}\n"
                            f"Phone:   {phone or '—'}\n"
                            f"Subject: {subject or '—'}\n\n"
                            f"Message:\n{'-' * 40}\n{message}\n{'-' * 40}"
                        )
                        html_body = (
                            "<html><body style='font-family:sans-serif;font-size:14px;color:#1e293b'>"
                            "<p>You have received a new contact form submission.</p>"
                            "<table style='border-collapse:collapse;margin-bottom:16px'>"
                            f"<tr><td style='padding:4px 12px 4px 0;font-weight:bold'>Name</td><td>{name}</td></tr>"
                            f"<tr><td style='padding:4px 12px 4px 0;font-weight:bold'>Email</td><td><a href='mailto:{email}'>{email}</a></td></tr>"
                            f"<tr><td style='padding:4px 12px 4px 0;font-weight:bold'>Phone</td><td>{phone or '—'}</td></tr>"
                            f"<tr><td style='padding:4px 12px 4px 0;font-weight:bold'>Subject</td><td>{subject or '—'}</td></tr>"
                            "</table>"
                            "<p style='font-weight:bold;margin-bottom:4px'>Message:</p>"
                            f"<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:12px 16px;white-space:pre-wrap'>{message}</div>"
                            "</body></html>"
                        )
                        send_mail(
                            subject=f"[Contact] {subject or 'New enquiry'} from {name}",
                            message=plain_body,
                            from_email=django_settings.DEFAULT_FROM_EMAIL,
                            recipient_list=[dest],
                            fail_silently=False,
                            html_message=html_body,
                        )
                    except Exception:
                        pass

            return render(
                request,
                "public/contact.html",
                {"form": ContactForm(), "settings": settings, "submitted": True},
            )
    else:
        form = ContactForm()

    return render(request, "public/contact.html", {"form": form, "settings": settings, "submitted": False})


def legal_page(request, slug):
    from apps.core.models import LegalPage

    page = get_object_or_404(LegalPage, slug=slug, is_published=True)
    return render(request, "public/legal_page.html", {"page": page})


def blog_list(request):
    from apps.core.models import BlogPost

    posts = BlogPost.objects.filter(status=BlogPost.STATUS_PUBLISHED).order_by("-published_at")
    return render(request, "public/blog_list.html", {"posts": posts})


def blog_detail(request, slug):
    from apps.core.models import BlogPost

    post = get_object_or_404(BlogPost, slug=slug, status=BlogPost.STATUS_PUBLISHED)
    return render(request, "public/blog_detail.html", {"post": post})


def handler404(request, exception):
    from apps.core.models import ErrorPageContent

    page = ErrorPageContent.objects.filter(status_code=ErrorPageContent.STATUS_404).first()
    return render(request, "404.html", {"page": page}, status=404)


def handler500(request):
    from apps.core.models import ErrorPageContent

    page = ErrorPageContent.objects.filter(status_code=ErrorPageContent.STATUS_500).first()
    return render(request, "500.html", {"page": page}, status=500)
