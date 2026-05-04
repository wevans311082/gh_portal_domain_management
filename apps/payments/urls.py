from django.urls import path
from . import views

app_name = "payments"

urlpatterns = [
    path("stripe/checkout/<int:invoice_id>/", views.stripe_checkout, name="stripe_checkout"),
    path("stripe/success/", views.stripe_success, name="stripe_success"),
    path("webhooks/stripe/", views.stripe_webhook, name="stripe_webhook"),
    path("webhooks/gocardless/", views.gocardless_webhook, name="gocardless_webhook"),
    # Saved payment methods
    path("cards/", views.saved_cards, name="saved_cards"),
    path("cards/add/", views.add_card, name="add_card"),
    path("cards/<int:pk>/delete/", views.delete_card, name="delete_card"),
    path("cards/<int:pk>/default/", views.set_default_card, name="set_default_card"),
]
