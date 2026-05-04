from django.urls import path
from . import views

app_name = "portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("shop/", views.shop, name="shop"),
    path("cart/", views.cart_detail, name="cart"),
    path("cart/add-hosting/", views.cart_add_hosting, name="cart_add_hosting"),
    path("cart/add-domain/", views.cart_add_domain, name="cart_add_domain"),
    path("cart/add-renewal/", views.cart_add_renewal, name="cart_add_renewal"),
    path("cart/add-transfer/", views.cart_add_transfer, name="cart_add_transfer"),
    path("cart/<int:pk>/remove/", views.cart_remove_item, name="cart_remove_item"),
    path("cart/checkout/invoice/", views.cart_checkout_invoice, name="cart_checkout_invoice"),
    path("cart/checkout/quote/", views.cart_checkout_quote, name="cart_checkout_quote"),
    path("services/", views.my_services, name="my_services"),
    path("quotes/", views.my_quotes, name="my_quotes"),
    path("statement/", views.account_statement, name="account_statement"),
    path("notifications/", views.notification_preferences, name="notification_preferences"),
    # Phase 6: Hosting self-service
    path("services/<int:service_pk>/sso/", views.hosting_sso, name="hosting_sso"),
    path("services/<int:service_pk>/usage/", views.hosting_usage, name="hosting_usage"),
    # Phase 7: Promo codes
    path("cart/promo/", views.apply_promo_code, name="apply_promo_code"),
    # Phase 8: Security / privacy
    path("account/login-history/", views.login_history, name="login_history"),
    path("account/data-export/", views.gdpr_data_export, name="gdpr_data_export"),
    # Phase 9: API keys
    path("account/api-keys/", views.api_key_list, name="api_key_list"),
    path("account/api-keys/new/", views.api_key_create, name="api_key_create"),
    path("account/api-keys/<int:pk>/revoke/", views.api_key_revoke, name="api_key_revoke"),
]
