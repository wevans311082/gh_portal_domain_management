from django.urls import path
from . import views

app_name = "payments"

urlpatterns = [
    path("stripe/checkout/<int:invoice_id>/", views.stripe_checkout, name="stripe_checkout"),
    path("stripe/success/", views.stripe_success, name="stripe_success"),
    path("webhooks/stripe/", views.stripe_webhook, name="stripe_webhook"),
    path("webhooks/gocardless/", views.gocardless_webhook, name="gocardless_webhook"),
]
