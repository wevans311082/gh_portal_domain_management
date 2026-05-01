from django.contrib import admin
from .models import CloudflareZone


@admin.register(CloudflareZone)
class CloudflareZoneAdmin(admin.ModelAdmin):
    list_display = ["domain", "zone_id", "is_active", "ssl_mode"]
    search_fields = ["domain__name", "zone_id"]
