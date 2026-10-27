from django.shortcuts import render
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_safe
from django.views.decorators.cache import never_cache
from .health import check_application_health


@require_safe
def health_check(request):
    state = check_application_health()
    response = JsonResponse(state, status=200 if state["status"] == "ok" else 503)
    response["X-Correlation-ID"] = state["correlation_id"]
    response["Cache-Control"] = "no-store"
    return response


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


@never_cache
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

            from apps.core.public_submissions import accept_contact, SubmissionConflict
            try:
                accept_contact(request, form.cleaned_data, settings)
            except (SubmissionConflict, ValueError) as error:
                form.add_error(None, str(error))
                return render(request, "public/contact.html", {"form": form, "settings": settings, "submitted": False},
                              status=409 if isinstance(error, SubmissionConflict) else 503)

            return render(
                request,
                "public/contact.html",
                {"form": ContactForm(), "settings": settings, "submitted": True},
            )
    else:
        from apps.core.public_submissions import new_submission_key
        form = ContactForm(initial={"submission_key": new_submission_key(request)})

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
