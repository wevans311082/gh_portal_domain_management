from django.contrib import admin
from .models import ProvisioningJob


@admin.register(ProvisioningJob)
class ProvisioningJobAdmin(admin.ModelAdmin):
    list_display = ["service", "status", "attempt_count", "max_attempts", "created_at"]
    list_filter = ["status"]
    search_fields = ["service__user__email", "idempotency_key"]
    readonly_fields = ["idempotency_key", "celery_task_id", "result_data"]
