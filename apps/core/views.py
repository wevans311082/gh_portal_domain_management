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
    return render(request, "public/contact.html")


def legal_page(request, slug):
    from apps.core.models import LegalPage

    page = get_object_or_404(LegalPage, slug=slug, is_published=True)
    return render(request, "public/legal_page.html", {"page": page})


def handler404(request, exception):
    from apps.core.models import ErrorPageContent

    page = ErrorPageContent.objects.filter(status_code=ErrorPageContent.STATUS_404).first()
    return render(request, "404.html", {"page": page}, status=404)


def handler500(request):
    from apps.core.models import ErrorPageContent

    page = ErrorPageContent.objects.filter(status_code=ErrorPageContent.STATUS_500).first()
    return render(request, "500.html", {"page": page}, status=500)
