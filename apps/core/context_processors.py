from django.conf import settings

from apps.core.runtime_settings import get_runtime_setting


def site_settings(request):
    return {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_DOMAIN": settings.SITE_DOMAIN,
        "STRIPE_PUBLISHABLE_KEY": get_runtime_setting("STRIPE_PUBLISHABLE_KEY", settings.STRIPE_PUBLISHABLE_KEY),
        "DJANGO_ADMIN_URL": getattr(settings, "DJANGO_ADMIN_URL", "admin/"),
    }
