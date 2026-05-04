from django.urls import path
from . import views

app_name = "portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("services/", views.my_services, name="my_services"),
    path("quotes/", views.my_quotes, name="my_quotes"),
]
