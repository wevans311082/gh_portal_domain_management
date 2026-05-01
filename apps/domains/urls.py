from django.urls import path
from . import views

app_name = "domains"

urlpatterns = [
    path("", views.domain_search, name="search"),
    path("check/", views.domain_check, name="check"),
    path("register/", views.domain_register, name="register"),
    path("my-domains/", views.my_domains, name="my_domains"),
    path("<int:pk>/", views.domain_detail, name="detail"),
    path("<int:pk>/toggle-autorenew/", views.domain_toggle_autorenew, name="toggle_autorenew"),
    path("<int:pk>/renew/", views.domain_renew, name="renew"),
    # Domain contacts
    path("contacts/", views.contact_list, name="contact_list"),
    path("contacts/create/", views.contact_create, name="contact_create"),
    path("contacts/<int:pk>/edit/", views.contact_edit, name="contact_edit"),
    path("contacts/<int:pk>/delete/", views.contact_delete, name="contact_delete"),
    path("contacts/<int:pk>/set-default/", views.contact_set_default, name="contact_set_default"),
]
