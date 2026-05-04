from django.conf import settings

from apps.domains.debug_state import get_entries
from apps.core.runtime_settings import get_runtime_setting


def site_settings(request):
    from apps.core.models import LegalPage, SiteContentSettings

    content_settings = SiteContentSettings.get_solo()
    legal_links = LegalPage.objects.filter(is_published=True, show_in_footer=True).order_by("sort_order", "title")

    return {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_DOMAIN": settings.SITE_DOMAIN,
        "STRIPE_PUBLISHABLE_KEY": get_runtime_setting("STRIPE_PUBLISHABLE_KEY", settings.STRIPE_PUBLISHABLE_KEY),
        "DJANGO_ADMIN_URL": getattr(settings, "DJANGO_ADMIN_URL", "admin/"),
        "RESELLERCLUB_DEBUG_MODE": str(get_runtime_setting("RESELLERCLUB_DEBUG_MODE", "false")).strip().lower() in ("1", "true", "yes", "on"),
        "RESELLERCLUB_DEBUG_ENTRIES": get_entries(),
        "CONTENT_SETTINGS": content_settings,
        "FOOTER_LEGAL_LINKS": legal_links,
    }


def announcement_banners(request):
    """Inject active announcement banners into all template contexts."""
    try:
        from apps.core.models import AnnouncementBanner
        banners = [b for b in AnnouncementBanner.objects.filter(is_active=True) if b.is_visible()]
        if request.user.is_authenticated and not request.user.is_staff:
            banners = [b for b in banners if not b.show_to_staff_only]
        return {"ANNOUNCEMENT_BANNERS": banners}
    except Exception:
        return {"ANNOUNCEMENT_BANNERS": []}
