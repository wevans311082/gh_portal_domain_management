from django.db import models
from apps.core.models import TimeStampedModel


class AuditLog(TimeStampedModel):
    user = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=255)
    model_name = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=100, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} - {self.action} at {self.created_at}"


class EmailLog(TimeStampedModel):
    recipient = models.EmailField()
    subject = models.CharField(max_length=255)
    template = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, default="sent")
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Email to {self.recipient}: {self.subject}"


class IPAllowlistEntry(TimeStampedModel):
    """IP addresses allowed to access staff/admin areas.

    When entries exist, only listed IPs may authenticate as staff.
    """

    ip_address = models.GenericIPAddressField(unique=True)
    label = models.CharField(max_length=100, blank=True, help_text="Friendly name, e.g. 'Office VPN'")
    is_active = models.BooleanField(default=True)
    added_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ip_allowlist_entries_added",
    )

    class Meta:
        ordering = ["ip_address"]
        verbose_name = "IP allowlist entry"
        verbose_name_plural = "IP allowlist entries"

    def __str__(self):
        return f"{self.ip_address} ({self.label})" if self.label else self.ip_address
