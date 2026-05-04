from django.db import models
from apps.core.models import TimeStampedModel


class NotificationTemplate(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)
    subject = models.CharField(max_length=255)
    html_content = models.TextField()
    text_content = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class NotificationPreference(TimeStampedModel):
    """Per-user opt-in/opt-out for each notification template."""

    from django.conf import settings

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    template_name = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)

    class Meta:
        unique_together = [("user", "template_name")]
        ordering = ["template_name"]

    def __str__(self):
        status = "on" if self.enabled else "off"
        return f"{self.user} · {self.template_name} ({status})"
