from django.db import models
from apps.core.models import TimeStampedModel


class Package(TimeStampedModel):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2)
    price_annually = models.DecimalField(max_digits=10, decimal_places=2)
    setup_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    whm_package_name = models.CharField(max_length=100, blank=True)
    disk_quota_mb = models.PositiveIntegerField(default=0)
    bandwidth_mb = models.PositiveIntegerField(default=0)
    email_accounts = models.PositiveIntegerField(default=0)
    databases = models.PositiveIntegerField(default=0)
    domains_allowed = models.PositiveIntegerField(default=1)
    subdomains_allowed = models.PositiveIntegerField(default=10)
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    stripe_price_monthly_id = models.CharField(max_length=255, blank=True)
    stripe_price_annually_id = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["sort_order", "price_monthly"]

    def __str__(self):
        return self.name

    def get_features(self):
        return self.features.filter(is_active=True)


class PackageFeature(TimeStampedModel):
    package = models.ForeignKey(Package, on_delete=models.CASCADE, related_name="features")
    text = models.CharField(max_length=255)
    is_positive = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return f"{self.package.name}: {self.text}"
