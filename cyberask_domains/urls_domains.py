from django.urls import include, path

from .urls_subdomains import (
    common_auth_patterns,
    common_core_aux_patterns,
    common_staff_patterns,
    common_support_patterns,
    with_debug_static,
)

urlpatterns = [
    *common_auth_patterns,
    *common_support_patterns,
    *common_staff_patterns,
    *common_core_aux_patterns,
    path("", include("apps.domains.urls")),
    path("portal/", include("apps.portal.urls")),
    path("billing/invoices/", include("apps.invoices.urls")),
    path("billing/payments/", include("apps.payments.urls")),
    path("quote/", include("apps.billing.public_urls")),
    path("products/", include("apps.products.urls")),
    path("dns/", include("apps.dns.urls")),
    path("hosting/", include("apps.provisioning.urls")),
]

urlpatterns = with_debug_static(urlpatterns)

handler404 = "apps.core.views.handler404"
handler500 = "apps.core.views.handler500"
