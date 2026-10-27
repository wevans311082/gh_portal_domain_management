from django.contrib import admin

from .models import IntegrationSetting, WizardProgress


@admin.register(IntegrationSetting)
class IntegrationSettingAdmin(admin.ModelAdmin):
    list_display = ["key", "is_secret", "updated_at"]
    list_filter = ["is_secret"]
    search_fields = ["key"]
    fields = ["key", "is_secret", "protected_value", "updated_at"]
    readonly_fields = fields

    @admin.display(description="Stored value")
    def protected_value(self, obj):
        return "Protected. Use the integration configuration screens to make changes."

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WizardProgress)
class WizardProgressAdmin(admin.ModelAdmin):
    list_display = ["finished", "updated_at"]
    readonly_fields = ["created_at", "updated_at"]
