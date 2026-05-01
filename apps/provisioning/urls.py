from django.urls import path
from . import views

app_name = "provisioning"

urlpatterns = [
    path("", views.service_list, name="service_list"),
    path("<int:service_id>/", views.service_detail, name="service_detail"),
    path("<int:service_id>/email/create/", views.email_create, name="email_create"),
    path("<int:service_id>/email/delete/", views.email_delete, name="email_delete"),
    path("<int:service_id>/database/create/", views.database_create, name="database_create"),
    path("jobs/", views.job_list, name="job_list"),
]
