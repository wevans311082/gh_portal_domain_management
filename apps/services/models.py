from django.db import models
from apps.core.models import TimeStampedModel
from apps.accounts.models import User
from apps.products.models import Package
from apps.provisioning.providers import PROVIDER_CHOICES


class Service(TimeStampedModel):
    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_SUSPENDED = "suspended"
    STATUS_CANCELLED = "cancelled"
    STATUS_TERMINATED = "terminated"
    STATUS_FAILED = "failed"
    STATUS_REVIEW = "review"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_SUSPENDED, "Suspended"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_TERMINATED, "Terminated"),
        (STATUS_FAILED, "Failed"),
        (STATUS_REVIEW, "Pending Review"),
    ]

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="services")
    invoice = models.ForeignKey(
        "billing.Invoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="services",
    )
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name="services")
    provisioning_provider = models.CharField(
        max_length=50,
        choices=[("", "Use package default")] + PROVIDER_CHOICES,
        blank=True,
        default="",
        help_text="Optional backend override for this individual service.",
    )
    provisioning_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Provider-specific configuration that overrides package provisioning config.",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    domain_name = models.CharField(max_length=255, blank=True)
    cpanel_username = models.CharField(max_length=16, blank=True)
    cpanel_domain = models.CharField(max_length=255, blank=True)
    cpanel_ip = models.GenericIPAddressField(null=True, blank=True)
    cpanel_server = models.CharField(max_length=255, blank=True)
    whm_last_sync_action = models.CharField(max_length=50, blank=True)
    whm_last_sync_at = models.DateTimeField(null=True, blank=True)
    whm_last_sync_ok = models.BooleanField(null=True, blank=True)
    whm_last_sync_message = models.TextField(blank=True)
    billing_period = models.CharField(
        max_length=20,
        choices=[("monthly", "Monthly"), ("annually", "Annually")],
        default="monthly",
    )
    next_due_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = "Service"
        verbose_name_plural = "Services"

    def __str__(self):
        return f"{self.user.email} - {self.package.name} ({self.status})"
