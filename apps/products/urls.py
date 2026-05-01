from django.urls import path
from . import views

app_name = "products"

urlpatterns = [
    path("", views.package_list, name="list"),
    path("<slug:slug>/", views.package_detail, name="detail"),
]
