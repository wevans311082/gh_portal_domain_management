from django.urls import path
from . import views

app_name = "domains"

urlpatterns = [
    path("", views.domain_search, name="search"),
    path("check/", views.domain_check, name="check"),
    path("whois/", views.domain_whois, name="whois"),
    path("register/", views.domain_register, name="register"),
    path("my-domains/", views.my_domains, name="my_domains"),
    path("my-domains/bulk-renew/", views.domain_bulk_add_to_cart, name="bulk_add_to_cart"),
    path("<int:pk>/", views.domain_detail, name="detail"),
    path("<int:pk>/toggle-autorenew/", views.domain_toggle_autorenew, name="toggle_autorenew"),
    path("<int:pk>/toggle-lock/", views.domain_toggle_lock, name="toggle_lock"),
    path("<int:pk>/get-auth-code/", views.domain_get_auth_code, name="get_auth_code"),
    path("<int:pk>/nameservers/", views.domain_update_nameservers, name="update_nameservers"),
    path("<int:pk>/renew/", views.domain_renew, name="renew"),
    # Domain contacts
    path("contacts/", views.contact_list, name="contact_list"),
    path("contacts/create/", views.contact_create, name="contact_create"),
    path("contacts/<int:pk>/edit/", views.contact_edit, name="contact_edit"),
    path("contacts/<int:pk>/delete/", views.contact_delete, name="contact_delete"),
    path("contacts/<int:pk>/set-default/", views.contact_set_default, name="contact_set_default"),
]
