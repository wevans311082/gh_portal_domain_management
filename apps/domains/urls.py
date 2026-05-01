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
]
