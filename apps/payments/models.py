from django.db import models
from apps.core.models import TimeStampedModel
from apps.accounts.models import User


class StripeCustomer(TimeStampedModel):
    """Maps a portal user to a Stripe Customer ID for saved payment methods."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="stripe_customer")
    stripe_customer_id = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return f"{self.user} → {self.stripe_customer_id}"


class SavedPaymentMethod(TimeStampedModel):
    """A card saved via Stripe SetupIntent, linked to a portal user."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="saved_payment_methods")
    stripe_pm_id = models.CharField(max_length=255, unique=True)
    last4 = models.CharField(max_length=4)
    brand = models.CharField(max_length=20)
    exp_month = models.PositiveSmallIntegerField()
    exp_year = models.PositiveSmallIntegerField()
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.brand} •••• {self.last4} (exp {self.exp_month}/{self.exp_year})"

    def save(self, *args, **kwargs):
        # Ensure only one default per user
        if self.is_default:
            SavedPaymentMethod.objects.filter(user=self.user, is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class Payment(TimeStampedModel):
    STATUS_PENDING = "pending"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_REFUNDED = "refunded"
    STATUS_PARTIAL_REFUND = "partial_refund"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_REFUNDED, "Refunded"),
        (STATUS_PARTIAL_REFUND, "Partial Refund"),
    ]

    PROVIDER_STRIPE = "stripe"
    PROVIDER_GOCARDLESS = "gocardless"
    PROVIDER_PAYPAL = "paypal"
    PROVIDER_MANUAL = "manual"
    PROVIDER_BANK_TRANSFER = "bank_transfer"

    PROVIDER_CHOICES = [
        (PROVIDER_STRIPE, "Stripe"),
        (PROVIDER_GOCARDLESS, "GoCardless"),
        (PROVIDER_PAYPAL, "PayPal"),
        (PROVIDER_MANUAL, "Manual"),
        (PROVIDER_BANK_TRANSFER, "Bank Transfer"),
    ]

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="payments")
    invoice = models.ForeignKey(
        "billing.Invoice",
        on_delete=models.PROTECT,
        related_name="payments",
        null=True,
        blank=True,
    )
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="GBP")
    external_id = models.CharField(max_length=255, blank=True)
    provider_data = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.provider} payment {self.external_id or self.id} - £{self.amount}"


class WebhookEvent(TimeStampedModel):
    provider = models.CharField(max_length=20)
    event_type = models.CharField(max_length=100)
    event_id = models.CharField(max_length=255, unique=True)
    payload = models.JSONField(default=dict)
    processed = models.BooleanField(default=False)
    processing_error = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.provider} {self.event_type} ({self.event_id})"
