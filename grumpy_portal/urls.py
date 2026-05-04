from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from apps.accounts import views as account_views

# Admin URL slug is configurable via DJANGO_ADMIN_URL env var (defaults to a
# non-guessable path; change it for every deployment).
_ADMIN_URL = getattr(settings, "DJANGO_ADMIN_URL", "manage-site-a3f7c2/")

urlpatterns = [
    path(_ADMIN_URL, admin.site.urls),
    path("accounts/login/", account_views.custom_login, name="account_login"),
    path("accounts/", include("allauth.urls")),
    path("my-account/", include("apps.accounts.urls")),
    path("", include("apps.core.urls")),
    path("portal/", include("apps.portal.urls")),
    path("products/", include("apps.products.urls")),
    path("billing/", include("apps.billing.urls")),
    path("quote/", include("apps.billing.public_urls")),
    path("invoices/", include("apps.invoices.urls")),
    path("payments/", include("apps.payments.urls")),
    path("domains/", include("apps.domains.urls")),
    path("dns/", include("apps.dns.urls")),
    path("support/", include("apps.support.urls")),
    path("hosting/", include("apps.provisioning.urls")),
    path("admin-tools/", include("apps.admin_tools.urls")),
    path("website-templates/", include("apps.website_templates.urls")),
]

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler404 = "apps.core.views.handler404"
handler500 = "apps.core.views.handler500"
