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


class HostingNode(TimeStampedModel):
    STATUS_ACTIVE = "active"
    STATUS_DRAINING = "draining"
    STATUS_OFFLINE = "offline"
    STATUS_MAINTENANCE = "maintenance"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_DRAINING, "Draining"),
        (STATUS_OFFLINE, "Offline"),
        (STATUS_MAINTENANCE, "Maintenance"),
    ]

    name = models.CharField(max_length=120, unique=True)
    hostname = models.CharField(max_length=255)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    daemon_url = models.URLField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Hosting node"
        verbose_name_plural = "Hosting nodes"

    def __str__(self):
        return self.name


class WebsiteRuntime(TimeStampedModel):
    RUNTIME_STATIC = "static"
    RUNTIME_PHP = "php"
    RUNTIME_NODE = "node"
    RUNTIME_PYTHON = "python"
    RUNTIME_CUSTOM = "custom"

    RUNTIME_TYPE_CHOICES = [
        (RUNTIME_STATIC, "Static"),
        (RUNTIME_PHP, "PHP"),
        (RUNTIME_NODE, "Node.js"),
        (RUNTIME_PYTHON, "Python"),
        (RUNTIME_CUSTOM, "Custom"),
    ]

    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_FAILED = "failed"
    STATUS_RETIRED = "retired"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_FAILED, "Failed"),
        (STATUS_RETIRED, "Retired"),
    ]

    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name="website_runtimes"
    )
    node = models.ForeignKey(
        HostingNode, on_delete=models.PROTECT, related_name="website_runtimes"
    )
    runtime_type = models.CharField(
        max_length=30, choices=RUNTIME_TYPE_CHOICES, default=RUNTIME_STATIC
    )
    image = models.CharField(max_length=255)
    image_tag = models.CharField(max_length=120, default="latest")
    document_root = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    last_deployed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["service", "runtime_type", "image"]
        verbose_name = "Website runtime"
        verbose_name_plural = "Website runtimes"
        constraints = [
            models.UniqueConstraint(
                fields=["service", "runtime_type", "image", "image_tag"],
                name="uniq_website_runtime_per_service_image",
            )
        ]

    def __str__(self):
        return f"{self.service} - {self.runtime_type} ({self.image}:{self.image_tag})"


class WebsiteContainer(TimeStampedModel):
    STATUS_CREATING = "creating"
    STATUS_RUNNING = "running"
    STATUS_STOPPED = "stopped"
    STATUS_UNHEALTHY = "unhealthy"
    STATUS_FAILED = "failed"
    STATUS_REMOVED = "removed"

    STATUS_CHOICES = [
        (STATUS_CREATING, "Creating"),
        (STATUS_RUNNING, "Running"),
        (STATUS_STOPPED, "Stopped"),
        (STATUS_UNHEALTHY, "Unhealthy"),
        (STATUS_FAILED, "Failed"),
        (STATUS_REMOVED, "Removed"),
    ]

    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name="website_containers"
    )
    node = models.ForeignKey(
        HostingNode, on_delete=models.PROTECT, related_name="website_containers"
    )
    runtime = models.ForeignKey(
        WebsiteRuntime,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="containers",
    )
    container_name = models.CharField(max_length=255, unique=True)
    internal_port = models.PositiveIntegerField(default=80)
    domain = models.CharField(max_length=255)
    document_root = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_CREATING)
    healthcheck_url = models.URLField(blank=True)
    last_deployed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["domain", "container_name"]
        verbose_name = "Website container"
        verbose_name_plural = "Website containers"
        constraints = [
            models.UniqueConstraint(
                fields=["node", "domain"], name="uniq_website_container_node_domain"
            )
        ]

    def __str__(self):
        return f"{self.domain} on {self.node} ({self.status})"
