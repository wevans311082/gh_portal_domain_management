from django.db import models
from apps.core.models import TimeStampedModel
from apps.services.models import Service


class ProvisioningJob(TimeStampedModel):
    STATUS_QUEUED = "queued"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_RETRY = "retry"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_RETRY, "Retrying"),
    ]

    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="provisioning_jobs")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    idempotency_key = models.CharField(max_length=255, unique=True)
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    last_error = models.TextField(blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    result_data = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Provisioning job for {self.service} ({self.status})"


class WHMSyncRun(TimeStampedModel):
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    package_count = models.PositiveIntegerField(default=0)
    account_count = models.PositiveIntegerField(default=0)
    usage_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    result_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"WHM sync #{self.pk} ({self.status})"


class WHMServerSnapshot(TimeStampedModel):
    host = models.CharField(max_length=255)
    server_version = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-synced_at"]

    def __str__(self):
        return f"WHM server snapshot for {self.host}"


class WHMPackageSnapshot(TimeStampedModel):
    name = models.CharField(max_length=120, unique=True)
    owner = models.CharField(max_length=120, blank=True)
    feature_list = models.CharField(max_length=120, blank=True)
    disk_quota_mb = models.CharField(max_length=64, blank=True)
    bandwidth_quota_mb = models.CharField(max_length=64, blank=True)
    max_email_accounts = models.CharField(max_length=64, blank=True)
    max_ftp_accounts = models.CharField(max_length=64, blank=True)
    max_databases = models.CharField(max_length=64, blank=True)
    max_subdomains = models.CharField(max_length=64, blank=True)
    max_parked_domains = models.CharField(max_length=64, blank=True)
    max_addon_domains = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class WHMAccountSnapshot(TimeStampedModel):
    username = models.CharField(max_length=64, unique=True)
    domain = models.CharField(max_length=255, blank=True)
    email = models.CharField(max_length=255, blank=True)
    owner = models.CharField(max_length=120, blank=True)
    plan = models.CharField(max_length=120, blank=True)
    ip = models.CharField(max_length=64, blank=True)
    server = models.CharField(max_length=255, blank=True)
    partition = models.CharField(max_length=255, blank=True)
    unix_start_date = models.CharField(max_length=120, blank=True)
    suspended = models.BooleanField(default=False)
    suspended_reason = models.TextField(blank=True)
    service = models.ForeignKey(
        Service,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whm_account_snapshots",
    )
    payload = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return self.username


class WHMAccountUsageSnapshot(TimeStampedModel):
    account = models.OneToOneField(
        WHMAccountSnapshot,
        on_delete=models.CASCADE,
        related_name="usage",
    )
    disk_used_mb = models.CharField(max_length=64, blank=True)
    disk_limit_mb = models.CharField(max_length=64, blank=True)
    disk_used_percent = models.CharField(max_length=64, blank=True)
    inode_used = models.CharField(max_length=64, blank=True)
    inode_limit = models.CharField(max_length=64, blank=True)
    inode_used_percent = models.CharField(max_length=64, blank=True)
    monthly_bandwidth_used_mb = models.CharField(max_length=64, blank=True)
    monthly_bandwidth_limit_mb = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["account__username"]

    def __str__(self):
        return f"Usage for {self.account.username}"
