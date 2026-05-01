from django.db import models
from apps.core.models import TimeStampedModel
from apps.domains.models import Domain


class DNSZone(TimeStampedModel):
    domain = models.OneToOneField(Domain, on_delete=models.CASCADE, related_name="dns_zone")
    provider = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)
    last_synced = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Zone: {self.domain.name}"


class DNSRecord(TimeStampedModel):
    TYPE_A = "A"
    TYPE_AAAA = "AAAA"
    TYPE_CNAME = "CNAME"
    TYPE_MX = "MX"
    TYPE_TXT = "TXT"
    TYPE_SRV = "SRV"
    TYPE_CAA = "CAA"
    TYPE_NS = "NS"

    RECORD_TYPES = [
        (TYPE_A, "A"),
        (TYPE_AAAA, "AAAA"),
        (TYPE_CNAME, "CNAME"),
        (TYPE_MX, "MX"),
        (TYPE_TXT, "TXT"),
        (TYPE_SRV, "SRV"),
        (TYPE_CAA, "CAA"),
        (TYPE_NS, "NS"),
    ]

    zone = models.ForeignKey(DNSZone, on_delete=models.CASCADE, related_name="records")
    record_type = models.CharField(max_length=10, choices=RECORD_TYPES)
    name = models.CharField(max_length=255)
    content = models.TextField()
    ttl = models.PositiveIntegerField(default=3600)
    priority = models.PositiveIntegerField(null=True, blank=True)
    proxied = models.BooleanField(default=False)
    external_id = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.record_type} {self.name} -> {self.content}"
