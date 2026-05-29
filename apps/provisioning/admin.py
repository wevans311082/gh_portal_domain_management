from django.contrib import admin
from .models import (
    HostingNode,
    WebsiteContainer,
    WebsiteRuntime,
    ProvisioningJob,
    WHMAccountSnapshot,
    WHMAccountUsageSnapshot,
    WHMPackageSnapshot,
    WHMServerSnapshot,
    WHMSyncRun,
)


@admin.register(ProvisioningJob)
class ProvisioningJobAdmin(admin.ModelAdmin):
    list_display = ["service", "status", "attempt_count", "max_attempts", "created_at"]
    list_filter = ["status"]
    search_fields = ["service__user__email", "idempotency_key"]
    readonly_fields = ["idempotency_key", "celery_task_id", "result_data"]


@admin.register(WHMSyncRun)
class WHMSyncRunAdmin(admin.ModelAdmin):
    list_display = ["id", "status", "package_count", "account_count", "usage_count", "error_count", "started_at", "finished_at"]
    list_filter = ["status"]
    readonly_fields = ["result_data", "last_error", "started_at", "finished_at", "created_at", "updated_at"]


@admin.register(WHMServerSnapshot)
class WHMServerSnapshotAdmin(admin.ModelAdmin):
    list_display = ["host", "server_version", "synced_at"]
    search_fields = ["host", "server_version"]
    readonly_fields = ["payload", "synced_at", "created_at", "updated_at"]


@admin.register(WHMPackageSnapshot)
class WHMPackageSnapshotAdmin(admin.ModelAdmin):
    list_display = ["name", "owner", "feature_list", "disk_quota_mb", "bandwidth_quota_mb", "is_active", "synced_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "owner", "feature_list"]
    readonly_fields = ["payload", "synced_at", "created_at", "updated_at"]


@admin.register(WHMAccountSnapshot)
class WHMAccountSnapshotAdmin(admin.ModelAdmin):
    list_display = ["username", "domain", "plan", "owner", "suspended", "is_active", "service", "synced_at"]
    list_filter = ["suspended", "is_active", "plan"]
    search_fields = ["username", "domain", "email", "plan", "owner", "service__user__email"]
    readonly_fields = ["payload", "synced_at", "created_at", "updated_at"]


@admin.register(WHMAccountUsageSnapshot)
class WHMAccountUsageSnapshotAdmin(admin.ModelAdmin):
    list_display = ["account", "disk_used_mb", "disk_limit_mb", "inode_used", "monthly_bandwidth_used_mb", "synced_at"]
    search_fields = ["account__username", "account__domain"]
    readonly_fields = ["payload", "synced_at", "created_at", "updated_at"]


@admin.register(HostingNode)
class HostingNodeAdmin(admin.ModelAdmin):
    list_display = ["name", "hostname", "ip_address", "status", "updated_at"]
    list_filter = ["status"]
    search_fields = ["name", "hostname", "ip_address", "daemon_url"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(WebsiteRuntime)
class WebsiteRuntimeAdmin(admin.ModelAdmin):
    list_display = [
        "service",
        "node",
        "runtime_type",
        "image",
        "image_tag",
        "status",
        "last_deployed_at",
    ]
    list_filter = ["runtime_type", "status", "node"]
    search_fields = [
        "service__user__email",
        "service__domain_name",
        "node__name",
        "image",
        "image_tag",
        "document_root",
    ]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(WebsiteContainer)
class WebsiteContainerAdmin(admin.ModelAdmin):
    list_display = [
        "container_name",
        "domain",
        "service",
        "node",
        "runtime",
        "internal_port",
        "status",
        "last_deployed_at",
    ]
    list_filter = ["status", "node", "runtime__runtime_type"]
    search_fields = [
        "container_name",
        "domain",
        "service__user__email",
        "service__domain_name",
        "node__name",
        "healthcheck_url",
    ]
    readonly_fields = ["created_at", "updated_at"]
