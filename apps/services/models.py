from django.db import models
from apps.core.models import TimeStampedModel
from apps.accounts.models import User
from apps.products.models import Package


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
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name="services")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    domain_name = models.CharField(max_length=255, blank=True)
    cpanel_username = models.CharField(max_length=16, blank=True)
    cpanel_domain = models.CharField(max_length=255, blank=True)
    cpanel_ip = models.GenericIPAddressField(null=True, blank=True)
    cpanel_server = models.CharField(max_length=255, blank=True)
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
