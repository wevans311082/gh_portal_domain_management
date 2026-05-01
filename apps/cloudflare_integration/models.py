from django.db import models
from apps.core.models import TimeStampedModel
from apps.domains.models import Domain


class CloudflareZone(TimeStampedModel):
    domain = models.OneToOneField(Domain, on_delete=models.CASCADE, related_name="cloudflare_zone")
    zone_id = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    ssl_mode = models.CharField(max_length=20, default="flexible")
    always_https = models.BooleanField(default=True)
    security_level = models.CharField(max_length=20, default="medium")

    def __str__(self):
        return f"Cloudflare zone for {self.domain.name}"
