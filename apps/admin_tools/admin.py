from django.contrib import admin

from .models import IntegrationSetting, WizardProgress


@admin.register(IntegrationSetting)
class IntegrationSettingAdmin(admin.ModelAdmin):
    list_display = ["key", "is_secret", "updated_at"]
    list_filter = ["is_secret"]
    search_fields = ["key"]


@admin.register(WizardProgress)
class WizardProgressAdmin(admin.ModelAdmin):
    list_display = ["finished", "updated_at"]
    readonly_fields = ["created_at", "updated_at"]
