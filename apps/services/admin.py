from django.contrib import admin
from .models import Service


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ["user", "package", "status", "domain_name", "provisioning_provider", "next_due_date"]
    list_filter = ["status", "billing_period", "provisioning_provider"]
    search_fields = ["user__email", "domain_name", "cpanel_username"]
    raw_id_fields = ["user", "package"]
