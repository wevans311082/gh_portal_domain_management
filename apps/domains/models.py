from django.db import models
from apps.core.models import TimeStampedModel
from apps.accounts.models import User


class Domain(TimeStampedModel):
    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_EXPIRED = "expired"
    STATUS_SUSPENDED = "suspended"
    STATUS_TRANSFERRED = "transferred"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_SUSPENDED, "Suspended"),
        (STATUS_TRANSFERRED, "Transferred"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    DNS_PROVIDER_REGISTRAR = "registrar"
    DNS_PROVIDER_CPANEL = "cpanel"
    DNS_PROVIDER_CLOUDFLARE = "cloudflare"
    DNS_PROVIDER_EXTERNAL = "external"

    DNS_CHOICES = [
        (DNS_PROVIDER_REGISTRAR, "Registrar"),
        (DNS_PROVIDER_CPANEL, "cPanel"),
        (DNS_PROVIDER_CLOUDFLARE, "Cloudflare"),
        (DNS_PROVIDER_EXTERNAL, "External"),
    ]

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="domains")
    name = models.CharField(max_length=255, unique=True)
    tld = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    registrar_id = models.CharField(max_length=255, blank=True)
    registered_at = models.DateField(null=True, blank=True)
    expires_at = models.DateField(null=True, blank=True)
    auto_renew = models.BooleanField(default=True)
    is_locked = models.BooleanField(default=True)
    dns_provider = models.CharField(max_length=20, choices=DNS_CHOICES, default=DNS_PROVIDER_REGISTRAR)
    cloudflare_zone_id = models.CharField(max_length=255, blank=True)
    nameserver1 = models.CharField(max_length=255, blank=True)
    nameserver2 = models.CharField(max_length=255, blank=True)
    nameserver3 = models.CharField(max_length=255, blank=True)
    nameserver4 = models.CharField(max_length=255, blank=True)
    epp_code = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
