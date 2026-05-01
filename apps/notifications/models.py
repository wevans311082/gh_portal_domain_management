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
