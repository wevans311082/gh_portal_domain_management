from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.accounts import views as account_views
from apps.core import views as core_views

_ADMIN_URL = getattr(settings, "DJANGO_ADMIN_URL", "manage-site-a3f7c2/")

common_auth_patterns = [
    path("accounts/login/", account_views.custom_login, name="account_login"),
    path("accounts/", include("allauth.urls")),
    path("my-account/", include("apps.accounts.urls")),
]

common_support_patterns = [
    path("support/", include("apps.support.urls")),
]

common_staff_patterns = [
    path(_ADMIN_URL, admin.site.urls),
    path("admin-tools/", include("apps.admin_tools.urls")),
]

# Split-host urlconfs already bind path("") to a service app, so they cannot
# include apps.core.urls (home would collide). Templates still reverse
# core:pricing / core:contact / core:legal_page / core:health_check.
common_core_aux_patterns = [
    path(
        "",
        include(
            (
                [
                    path("home/", core_views.home, name="home"),
                    path("pricing/", core_views.pricing, name="pricing"),
                    path("contact/", core_views.contact, name="contact"),
                    path("legal/<slug:slug>/", core_views.legal_page, name="legal_page"),
                    path("blog/", core_views.blog_list, name="blog_list"),
                    path("blog/<slug:slug>/", core_views.blog_detail, name="blog_detail"),
                    path("health/", core_views.health_check, name="health_check"),
                ],
                "core",
            )
        ),
    ),
]


def with_debug_static(urlpatterns):
    if settings.DEBUG:
        import debug_toolbar

        urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns
        urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    return urlpatterns
