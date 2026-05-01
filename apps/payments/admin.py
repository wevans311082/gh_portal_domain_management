from django.contrib import admin
from .models import Payment, WebhookEvent


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ["user", "provider", "status", "amount", "currency", "created_at"]
    list_filter = ["provider", "status", "currency"]
    search_fields = ["user__email", "external_id"]
    raw_id_fields = ["user", "invoice"]


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ["provider", "event_type", "event_id", "processed", "created_at"]
    list_filter = ["provider", "processed"]
    search_fields = ["event_id", "event_type"]
