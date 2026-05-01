from django.urls import path
from . import views

app_name = "dns"

urlpatterns = [
    path("<int:domain_pk>/", views.zone_detail, name="zone_detail"),
]
