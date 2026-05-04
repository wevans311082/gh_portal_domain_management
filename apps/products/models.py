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
    is_quotable = models.BooleanField(
        default=False,
        help_text="Show this package in the public 'Build a quote' catalogue.",
    )
    quote_blurb = models.TextField(
        blank=True,
        help_text="Short marketing description shown on the public quote builder card.",
    )
    quote_category = models.CharField(
        max_length=50,
        blank=True,
        help_text="Free-text category label for grouping in the public quote builder (e.g. Hosting, Email, Security).",
    )
    show_on_homepage = models.BooleanField(
        default=True,
        help_text="Show this package card on the public homepage.",
    )
    card_blurb = models.CharField(
        max_length=180,
        blank=True,
        help_text="Short subtitle for the homepage package card.",
    )
    card_badge = models.CharField(
        max_length=40,
        blank=True,
        help_text="Optional badge text (e.g. Most Popular).",
    )
    card_cta_label = models.CharField(max_length=60, default="Get Started")
    card_sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["card_sort_order", "sort_order", "price_monthly"]

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
