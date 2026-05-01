from django.db import models
from apps.core.models import TimeStampedModel
from apps.accounts.models import User


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
