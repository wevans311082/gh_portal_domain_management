from django.contrib import admin
from .models import AuditLog, EmailLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["user", "action", "model_name", "object_id", "ip_address", "created_at"]
    list_filter = ["model_name"]
    search_fields = ["user__email", "action"]
    readonly_fields = ["user", "action", "model_name", "object_id", "ip_address", "user_agent", "data", "created_at"]


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ["recipient", "subject", "template", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["recipient", "subject"]
