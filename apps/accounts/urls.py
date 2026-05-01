from django.urls import path
from . import views

app_name = "accounts_custom"

urlpatterns = [
    path("register/", views.register, name="register"),
    path("login/", views.custom_login, name="login"),
    path("mfa/verify/", views.mfa_verify, name="mfa_verify"),
    path("profile/", views.profile, name="profile"),
    path("mfa/setup/", views.mfa_setup, name="mfa_setup"),
    path("mfa/disable/", views.mfa_disable, name="mfa_disable"),
    path("delete/", views.account_delete, name="account_delete"),
]
