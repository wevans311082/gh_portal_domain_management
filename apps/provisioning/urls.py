from django.urls import path
from . import views

app_name = "provisioning"

urlpatterns = [
    path("", views.job_list, name="list"),
]
