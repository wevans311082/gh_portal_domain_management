from django.contrib import admin

from .models import TemplateInstallation, WebsiteTemplate


@admin.register(WebsiteTemplate)
class WebsiteTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "is_active", "is_sanitised", "jquery_version", "bootstrap_version", "created_at"]
    list_filter = ["category", "is_active", "is_sanitised"]
    search_fields = ["name", "slug", "zip_filename"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ["created_at", "updated_at", "security_notes"]
    list_editable = ["is_active", "is_sanitised"]
    ordering = ["category", "name"]
    fieldsets = (
        (None, {"fields": ("name", "slug", "category", "description", "is_active")}),
        ("File system", {"fields": ("zip_filename", "extracted_path", "has_index")}),
        ("Security audit", {"fields": ("security_notes", "jquery_version", "bootstrap_version", "is_sanitised")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(TemplateInstallation)
class TemplateInstallationAdmin(admin.ModelAdmin):
    list_display = ["user", "template", "service_domain", "status", "installed_at"]
    list_filter = ["status"]
    search_fields = ["user__email", "template__name", "service_domain"]
    raw_id_fields = ["user", "template"]
