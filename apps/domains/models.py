from decimal import Decimal, ROUND_HALF_UP

from django.db import models
from apps.core.models import TimeStampedModel
from apps.accounts.models import User


def _default_supported_tlds():
    return ["co.uk", "com", "uk", "org", "net", "io", "org.uk"]


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

    @property
    def registrant_validation_status(self) -> str:
        try:
            order = self.order
        except DomainOrder.DoesNotExist:
            return DomainContact.VALIDATION_UNVALIDATED
        if not order or not order.registration_contact:
            return DomainContact.VALIDATION_UNVALIDATED
        return order.registration_contact.registrant_validation_status

    @property
    def is_registrant_validated(self) -> bool:
        return self.registrant_validation_status == DomainContact.VALIDATION_VALIDATED


class DomainPricingSettings(TimeStampedModel):
    default_profit_margin_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("25.00"))
    sync_enabled = models.BooleanField(default=True)
    sync_interval_hours = models.PositiveSmallIntegerField(default=12)
    supported_tlds = models.JSONField(default=_default_supported_tlds)
    last_sync_started_at = models.DateTimeField(null=True, blank=True)
    last_sync_completed_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.TextField(blank=True)

    class Meta:
        verbose_name = "Domain pricing settings"
        verbose_name_plural = "Domain pricing settings"

    def __str__(self):
        return "Domain pricing settings"

    @classmethod
    def get_solo(cls):
        obj = cls.objects.order_by("id").first()
        if obj:
            return obj
        return cls.objects.create()


class TLDPricing(TimeStampedModel):
    tld = models.CharField(max_length=50, unique=True)
    currency = models.CharField(max_length=3, default="GBP")
    registration_cost = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    renewal_cost = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    transfer_cost = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    profit_margin_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["tld"]
        verbose_name = "TLD pricing"
        verbose_name_plural = "TLD pricing"

    def __str__(self):
        return self.tld

    @staticmethod
    def _apply_margin(base_cost: Decimal, margin_percentage: Decimal) -> Decimal:
        multiplier = Decimal("1.00") + (margin_percentage / Decimal("100.00"))
        return (base_cost * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def effective_profit_margin_percentage(self) -> Decimal:
        if self.profit_margin_percentage is not None:
            return self.profit_margin_percentage
        return DomainPricingSettings.get_solo().default_profit_margin_percentage

    @property
    def registration_price(self) -> Decimal:
        return self._apply_margin(self.registration_cost, self.effective_profit_margin_percentage)

    @property
    def renewal_price(self) -> Decimal:
        return self._apply_margin(self.renewal_cost, self.effective_profit_margin_percentage)

    @property
    def transfer_price(self) -> Decimal:
        return self._apply_margin(self.transfer_cost, self.effective_profit_margin_percentage)


class DomainContact(TimeStampedModel):
    VALIDATION_UNVALIDATED = "unvalidated"
    VALIDATION_PENDING = "pending"
    VALIDATION_VALIDATED = "validated"
    VALIDATION_REJECTED = "rejected"

    VALIDATION_CHOICES = [
        (VALIDATION_UNVALIDATED, "Unvalidated"),
        (VALIDATION_PENDING, "Pending review"),
        (VALIDATION_VALIDATED, "Validated"),
        (VALIDATION_REJECTED, "Rejected"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="domain_contacts")
    label = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    company = models.CharField(max_length=255, blank=True)
    company_number = models.CharField(max_length=20, blank=True)
    email = models.EmailField()
    phone_country_code = models.CharField(max_length=8, default="44")
    phone = models.CharField(max_length=32)
    address_line1 = models.CharField(max_length=255)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    postcode = models.CharField(max_length=20)
    country = models.CharField(max_length=2, default="GB")
    is_default = models.BooleanField(default=False)
    registrar_contact_id = models.CharField(max_length=255, blank=True)
    registrant_validation_status = models.CharField(
        max_length=20,
        choices=VALIDATION_CHOICES,
        default=VALIDATION_UNVALIDATED,
    )
    registrant_validated_at = models.DateTimeField(null=True, blank=True)
    registrant_validation_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["user__email", "label"]
        verbose_name = "Domain contact"
        verbose_name_plural = "Domain contacts"

    def __str__(self):
        return f"{self.label} - {self.email}"

    @property
    def is_registrant_validated(self) -> bool:
        return self.registrant_validation_status == self.VALIDATION_VALIDATED

    def contact_type_for_tld(self, tld: str = "") -> str:
        """LogicBoxes contact type — required by contacts/add.json."""
        tld = (tld or "").strip().lower().lstrip(".")
        if tld in {"uk", "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk"}:
            return "UkContact"
        if tld == "ca":
            return "CaContact"
        if tld == "eu":
            return "EuContact"
        if tld == "de":
            return "DeContact"
        if tld == "us":
            return "Contact"
        return "Contact"

    def as_resellerclub_payload(self, customer_id: str, tld: str = "") -> dict:
        """Build contacts/add payload with required LogicBoxes fields.

        ResellerClub frequently returns HTTP 500 when ``type`` is missing or
        ``company`` is empty — treat those as hard requirements.
        """
        import re

        name = (self.name or "").strip() or "Registrant"
        company = (self.company or "").strip() or "N/A"
        email = (self.email or "").strip()
        address1 = (self.address_line1 or "").strip() or "Address not provided"
        city = (self.city or "").strip() or "London"
        state = (self.state or "").strip() or city
        zipcode = (self.postcode or "").strip() or "00000"
        country = (self.country or "GB").strip().upper()[:2]
        phone_cc = re.sub(r"\D", "", str(self.phone_country_code or "44")) or "44"
        phone = re.sub(r"\D", "", str(self.phone or ""))
        # LogicBoxes: phone 4–12 digits; strip leading 0 after UK country code
        if phone_cc == "44" and phone.startswith("0"):
            phone = phone[1:]
        if len(phone) < 4:
            phone = (phone + "0000")[:4]
        phone = phone[:12]

        return {
            "customer-id": str(customer_id).strip(),
            "type": self.contact_type_for_tld(tld),
            "name": name[:255],
            "company": company[:255],
            "email": email,
            "address-line-1": address1[:64],
            "address-line-2": (self.address_line2 or "")[:64],
            "city": city[:64],
            "state": state[:64],
            "zipcode": zipcode[:16],
            "country": country,
            "phone-cc": phone_cc[:3],
            "phone": phone,
        }


class DomainOrder(TimeStampedModel):
    STATUS_DRAFT = "draft"
    STATUS_PENDING_PAYMENT = "pending_payment"
    STATUS_PAID = "paid"
    STATUS_PROCESSING = "processing"
    STATUS_PAUSED = "paused"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PENDING_PAYMENT, "Pending payment"),
        (STATUS_PAID, "Paid"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_PAUSED, "Paused"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    # Platform-side open work (not completed/cancelled at registrar handoff)
    OPEN_STATUSES = (
        STATUS_DRAFT,
        STATUS_PENDING_PAYMENT,
        STATUS_PAID,
        STATUS_PROCESSING,
        STATUS_PAUSED,
        STATUS_FAILED,
    )

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="domain_orders")
    invoice = models.ForeignKey("billing.Invoice", on_delete=models.PROTECT, related_name="domain_orders", null=True, blank=True)
    domain = models.OneToOneField(Domain, on_delete=models.SET_NULL, related_name="order", null=True, blank=True)
    domain_name = models.CharField(max_length=255, unique=True)
    tld = models.CharField(max_length=50)
    registration_years = models.PositiveSmallIntegerField(default=1)
    quoted_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    privacy_enabled = models.BooleanField(default=True)
    auto_renew = models.BooleanField(default=True)
    dns_provider = models.CharField(max_length=20, choices=Domain.DNS_CHOICES, default=Domain.DNS_PROVIDER_CPANEL)
    registration_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="registration_orders")
    admin_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="admin_orders")
    tech_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="tech_orders")
    billing_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="billing_orders")
    registrar_order_id = models.CharField(max_length=255, blank=True)
    last_error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Domain order"
        verbose_name_plural = "Domain orders"

    def __str__(self):
        return f"{self.domain_name} ({self.status})"


class DomainRenewal(TimeStampedModel):
    """Tracks a single renewal event for a domain."""

    STATUS_PENDING_PAYMENT = "pending_payment"
    STATUS_PAID = "paid"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_PENDING_PAYMENT, "Pending payment"),
        (STATUS_PAID, "Paid"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    domain = models.ForeignKey(Domain, on_delete=models.PROTECT, related_name="renewals")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="domain_renewals")
    invoice = models.ForeignKey(
        "billing.Invoice",
        on_delete=models.PROTECT,
        related_name="domain_renewals",
        null=True,
        blank=True,
    )
    renewal_years = models.PositiveSmallIntegerField(default=1)
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING_PAYMENT)
    # New expiry date after successful renewal
    new_expiry_date = models.DateField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Domain renewal"
        verbose_name_plural = "Domain renewals"

    def __str__(self):
        return f"Renewal: {self.domain.name} ({self.status})"


class DomainTransfer(TimeStampedModel):
    STATUS_PENDING_PAYMENT = "pending_payment"
    STATUS_PAID = "paid"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_PENDING_PAYMENT, "Pending payment"),
        (STATUS_PAID, "Paid"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="domain_transfers")
    invoice = models.ForeignKey(
        "billing.Invoice",
        on_delete=models.PROTECT,
        related_name="domain_transfers",
        null=True,
        blank=True,
    )
    domain = models.OneToOneField(Domain, on_delete=models.SET_NULL, related_name="transfer", null=True, blank=True)
    domain_name = models.CharField(max_length=255, unique=True)
    tld = models.CharField(max_length=50)
    auth_code = models.CharField(max_length=255, blank=True)
    quoted_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING_PAYMENT)
    auto_renew = models.BooleanField(default=True)
    dns_provider = models.CharField(max_length=20, choices=Domain.DNS_CHOICES, default=Domain.DNS_PROVIDER_CPANEL)
    registration_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="registration_transfers")
    admin_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="admin_transfers")
    tech_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="tech_transfers")
    billing_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="billing_transfers")
    registrar_order_id = models.CharField(max_length=255, blank=True)
    last_error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Domain transfer"
        verbose_name_plural = "Domain transfers"

    def __str__(self):
        return f"Transfer: {self.domain_name} ({self.status})"
