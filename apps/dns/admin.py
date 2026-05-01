from django.contrib import admin
from .models import DNSZone, DNSRecord


class DNSRecordInline(admin.TabularInline):
    model = DNSRecord
    extra = 1


@admin.register(DNSZone)
class DNSZoneAdmin(admin.ModelAdmin):
    list_display = ["domain", "provider", "is_active", "last_synced"]
    inlines = [DNSRecordInline]


@admin.register(DNSRecord)
class DNSRecordAdmin(admin.ModelAdmin):
    list_display = ["zone", "record_type", "name", "content", "ttl"]
    list_filter = ["record_type", "is_active"]
