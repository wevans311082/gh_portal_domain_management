from django.urls import path
from . import views

app_name = "dns"

urlpatterns = [
    path("<int:domain_pk>/", views.zone_detail, name="zone_detail"),
    path("<int:domain_pk>/add/", views.record_add, name="record_add"),
    path("<int:domain_pk>/edit/<int:record_pk>/", views.record_edit, name="record_edit"),
    path("<int:domain_pk>/delete/<int:record_pk>/", views.record_delete, name="record_delete"),
]

