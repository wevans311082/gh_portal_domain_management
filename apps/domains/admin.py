from django.contrib import admin
from .models import Domain


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ["name", "user", "status", "expires_at", "auto_renew", "dns_provider"]
    list_filter = ["status", "dns_provider", "auto_renew"]
    search_fields = ["name", "user__email"]
    raw_id_fields = ["user"]
