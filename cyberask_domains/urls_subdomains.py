from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.accounts import views as account_views

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


def with_debug_static(urlpatterns):
    if settings.DEBUG:
        import debug_toolbar

        urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns
        urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    return urlpatterns
