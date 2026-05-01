from django.urls import path
from . import views

app_name = "admin_tools"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
]
