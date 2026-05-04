from django.contrib import admin
from .models import Domain, DomainContact, DomainOrder, DomainPricingSettings, TLDPricing


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ["name", "user", "status", "expires_at", "auto_renew", "dns_provider"]
    list_filter = ["status", "dns_provider", "auto_renew"]
    search_fields = ["name", "user__email"]
    raw_id_fields = ["user"]


@admin.register(TLDPricing)
class TLDPricingAdmin(admin.ModelAdmin):
    list_display = [
        "tld",
        "currency",
        "registration_cost",
        "registration_price_display",
        "renewal_cost",
        "renewal_price_display",
        "profit_margin_percentage",
        "last_synced_at",
        "is_active",
    ]
    list_filter = ["currency", "is_active"]
    search_fields = ["tld"]
    readonly_fields = ["last_synced_at", "last_sync_payload"]
    actions = ["queue_pricing_sync"]

    @admin.display(description="Sell price")
    def registration_price_display(self, obj):
        return obj.registration_price

    @admin.display(description="Renewal price")
    def renewal_price_display(self, obj):
        return obj.renewal_price

    @admin.action(description="Queue pricing sync for selected TLDs")
    def queue_pricing_sync(self, request, queryset):
        from apps.domains.tasks import sync_tld_pricing

        tlds = list(queryset.values_list("tld", flat=True))
        sync_tld_pricing.delay(tlds=tlds)
        self.message_user(request, f"Queued pricing sync for {len(tlds)} TLD(s).")


@admin.register(DomainPricingSettings)
class DomainPricingSettingsAdmin(admin.ModelAdmin):
    list_display = [
        "default_profit_margin_percentage",
        "sync_enabled",
        "sync_interval_hours",
        "last_sync_completed_at",
    ]
    readonly_fields = ["last_sync_started_at", "last_sync_completed_at", "last_sync_error"]

    def has_add_permission(self, request):
        if DomainPricingSettings.objects.exists():
            return False
        return super().has_add_permission(request)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from apps.domains.tasks import ensure_tld_pricing_sync_schedule

        ensure_tld_pricing_sync_schedule(obj)


@admin.register(DomainContact)
class DomainContactAdmin(admin.ModelAdmin):
    list_display = [
        "label",
        "user",
        "email",
        "country",
        "registrant_validation_status",
        "registrant_validated_at",
        "registrar_contact_id",
        "is_default",
    ]
    list_filter = ["country", "is_default", "registrant_validation_status"]
    search_fields = ["label", "email", "user__email", "name", "company", "company_number"]
    raw_id_fields = ["user"]


@admin.register(DomainOrder)
class DomainOrderAdmin(admin.ModelAdmin):
    list_display = ["domain_name", "user", "status", "dns_provider", "total_price", "registrar_order_id", "completed_at"]
    list_filter = ["status", "dns_provider", "auto_renew", "privacy_enabled"]
    search_fields = ["domain_name", "user__email", "registrar_order_id"]
    raw_id_fields = ["user", "invoice", "domain", "registration_contact", "admin_contact", "tech_contact", "billing_contact"]
