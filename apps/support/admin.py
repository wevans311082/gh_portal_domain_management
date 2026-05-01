from django.contrib import admin
from .models import Department, SupportTicket, SupportTicketMessage


class SupportTicketMessageInline(admin.TabularInline):
    model = SupportTicketMessage
    extra = 1


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ["name", "email", "is_active"]


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "subject", "status", "priority", "assigned_to", "created_at"]
    list_filter = ["status", "priority", "department"]
    search_fields = ["user__email", "subject"]
    raw_id_fields = ["user", "assigned_to"]
    inlines = [SupportTicketMessageInline]
