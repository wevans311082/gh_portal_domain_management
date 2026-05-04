"""Billing models: invoices, quotes, line items, and document branding."""
import uuid
from decimal import Decimal

from django.db import models

from apps.accounts.models import User
from apps.core.models import TimeStampedModel


# ---------------------------------------------------------------------------
# Document branding (singleton) — drives invoice + quote look & feel.
# ---------------------------------------------------------------------------


def _default_invoice_number_format() -> str:
    return "INV-{yyyy}-{seq:05d}"


def _default_quote_number_format() -> str:
    return "QTE-{yyyy}-{seq:05d}"


class BillingDocumentBranding(TimeStampedModel):
    """Singleton holding the brand & defaults shared by every invoice/quote.

    Loaded via ``BillingDocumentBranding.get_solo()`` and edited from the
    admin tools UI. Anything that varies between deployments lives here so
    we never have to redeploy code to change a logo, VAT rate, footer text,
    or invoice numbering scheme.
    """

    company_name = models.CharField(max_length=255, default="Grumpy Hosting")
    registered_address = models.TextField(blank=True)
    company_number = models.CharField(max_length=64, blank=True)
    vat_number = models.CharField(max_length=64, blank=True)
    support_email = models.EmailField(blank=True)
    support_phone = models.CharField(max_length=64, blank=True)
    website_url = models.URLField(blank=True)

    logo = models.FileField(upload_to="billing/branding/", blank=True, null=True)
    accent_colour = models.CharField(max_length=9, default="#0ea5e9", help_text="Hex e.g. #0ea5e9")

    # Free text used in PDF + email templates.
    header_text = models.TextField(blank=True, help_text="Optional intro text shown above line items")
    footer_text = models.TextField(blank=True, help_text="Shown at the bottom of every page")
    legal_text = models.TextField(blank=True, help_text="Terms / payment terms / legal small print")
    signature_block = models.TextField(blank=True)

    # Defaults applied when a caller of BillingService doesn't pass overrides.
    default_currency = models.CharField(max_length=3, default="GBP")
    default_vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("20.00"))
    default_due_days = models.PositiveSmallIntegerField(default=14)
    default_quote_validity_days = models.PositiveSmallIntegerField(default=30)

    # Numbering. ``{yyyy}`` and ``{seq}`` are the two supported placeholders.
    invoice_number_format = models.CharField(max_length=64, default=_default_invoice_number_format)
    quote_number_format = models.CharField(max_length=64, default=_default_quote_number_format)
    invoice_seq = models.PositiveIntegerField(default=0)
    quote_seq = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Billing document branding"
        verbose_name_plural = "Billing document branding"

    def __str__(self):
        return "Billing document branding"

    @classmethod
    def get_solo(cls):
        obj = cls.objects.order_by("id").first()
        if obj:
            return obj
        return cls.objects.create()


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------


class Invoice(TimeStampedModel):
    STATUS_DRAFT = "draft"
    STATUS_UNPAID = "unpaid"
    STATUS_PAID = "paid"
    STATUS_VOID = "void"
    STATUS_OVERDUE = "overdue"
    STATUS_PARTIALLY_PAID = "partially_paid"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_UNPAID, "Unpaid"),
        (STATUS_PAID, "Paid"),
        (STATUS_VOID, "Void"),
        (STATUS_OVERDUE, "Overdue"),
        (STATUS_PARTIALLY_PAID, "Partially Paid"),
    ]

    SOURCE_DOMAIN_REGISTRATION = "domain_registration"
    SOURCE_DOMAIN_RENEWAL = "domain_renewal"
    SOURCE_AUTO_RENEWAL = "auto_renewal"
    SOURCE_SERVICE_ORDER = "service_order"
    SOURCE_QUOTE_ACCEPTANCE = "quote_acceptance"
    SOURCE_MANUAL_ADMIN = "manual_admin"
    SOURCE_STRIPE_SUBSCRIPTION = "stripe_subscription"

    SOURCE_CHOICES = [
        (SOURCE_DOMAIN_REGISTRATION, "Domain registration"),
        (SOURCE_DOMAIN_RENEWAL, "Domain renewal"),
        (SOURCE_AUTO_RENEWAL, "Auto-renewal"),
        (SOURCE_SERVICE_ORDER, "Service order"),
        (SOURCE_QUOTE_ACCEPTANCE, "Quote acceptance"),
        (SOURCE_MANUAL_ADMIN, "Manual / admin"),
        (SOURCE_STRIPE_SUBSCRIPTION, "Stripe subscription"),
    ]

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="invoices")
    number = models.CharField(max_length=50, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    source_kind = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_MANUAL_ADMIN)
    currency = models.CharField(max_length=3, default="GBP")
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    due_date = models.DateField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    billing_name = models.CharField(max_length=255, blank=True)
    billing_address = models.TextField(blank=True)
    stripe_invoice_id = models.CharField(max_length=255, blank=True)
    created_by_staff = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices_created",
    )
    source_quote = models.ForeignKey(
        "billing.Quote",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="converted_invoices",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invoice #{self.number}"

    def calculate_totals(self):
        self.subtotal = sum((item.line_total for item in self.line_items.all()), Decimal("0.00"))
        self.vat_amount = (self.subtotal * self.vat_rate / Decimal("100")).quantize(Decimal("0.01"))
        self.total = self.subtotal + self.vat_amount
        self.save(update_fields=["subtotal", "vat_amount", "total"])

    @property
    def amount_outstanding(self):
        return self.total - self.amount_paid

    @property
    def is_editable(self) -> bool:
        """Staff can only edit before a payment lands or after voiding."""
        return self.status in (
            self.STATUS_DRAFT,
            self.STATUS_UNPAID,
            self.STATUS_OVERDUE,
            self.STATUS_PARTIALLY_PAID,
        )


class InvoiceLineItem(TimeStampedModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="line_items")
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def save(self, *args, **kwargs):
        self.line_total = (self.quantity * self.unit_price).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    def __str__(self):
        return self.description


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------


class Quote(TimeStampedModel):
    STATUS_DRAFT = "draft"
    STATUS_SENT = "sent"
    STATUS_VIEWED = "viewed"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    STATUS_EXPIRED = "expired"
    STATUS_CONVERTED = "converted"
    STATUS_VOID = "void"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_SENT, "Sent"),
        (STATUS_VIEWED, "Viewed"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_CONVERTED, "Converted to invoice"),
        (STATUS_VOID, "Void"),
    ]

    number = models.CharField(max_length=50, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quotes",
    )
    # Lead capture for public/anonymous quotes.
    lead_email = models.EmailField(blank=True)
    lead_name = models.CharField(max_length=255, blank=True)
    lead_company = models.CharField(max_length=255, blank=True)
    lead_phone = models.CharField(max_length=64, blank=True)

    currency = models.CharField(max_length=3, default="GBP")
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    valid_until = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, help_text="Visible to the customer")
    internal_notes = models.TextField(blank=True, help_text="Staff-only")

    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by_ip = models.GenericIPAddressField(null=True, blank=True)
    converted_invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originating_quote",
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quotes_created",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Quote #{self.number}"

    def calculate_totals(self):
        self.subtotal = sum((item.line_total for item in self.line_items.all()), Decimal("0.00"))
        self.vat_amount = (self.subtotal * self.vat_rate / Decimal("100")).quantize(Decimal("0.01"))
        self.total = self.subtotal + self.vat_amount
        self.save(update_fields=["subtotal", "vat_amount", "total"])

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone as tz
        if not self.valid_until:
            return False
        return tz.now().date() > self.valid_until

    @property
    def is_acceptable(self) -> bool:
        """True if a customer can still accept this quote."""
        return self.status in (self.STATUS_SENT, self.STATUS_VIEWED) and not self.is_expired

    @property
    def is_editable(self) -> bool:
        return self.status in (self.STATUS_DRAFT, self.STATUS_SENT, self.STATUS_VIEWED)

    @property
    def display_recipient(self) -> str:
        if self.lead_name:
            return self.lead_name
        if self.user:
            return getattr(self.user, "full_name", "") or self.user.email
        return self.lead_email or ""


class QuoteLineItem(TimeStampedModel):
    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="line_items")
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def save(self, *args, **kwargs):
        self.line_total = (self.quantity * self.unit_price).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    def __str__(self):
        return self.description
