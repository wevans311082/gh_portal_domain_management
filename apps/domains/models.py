from apps.admin_tools.search_keys import SearchKey
from decimal import Decimal, ROUND_HALF_UP
import uuid

from django.db import models
from django.utils import timezone
from apps.core.models import TimeStampedModel
from apps.accounts.models import User


def _default_supported_tlds():
    return ["co.uk", "com", "uk", "org", "net", "io", "org.uk"]


class ImmutableAgreementRecord(TimeStampedModel):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            from django.core.exceptions import ValidationError
            raise ValidationError("Published agreements and acceptance records cannot be edited. Record a new version.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        raise ValidationError("Agreement history must be retained.")


class RegistrarAgreementVersion(ImmutableAgreementRecord):
    agreement_key = models.SlugField(max_length=80)
    version = models.CharField(max_length=80)
    title = models.CharField(max_length=200)
    body = models.TextField()
    source_url = models.URLField(max_length=500, blank=True)
    profile_keys = models.JSONField(default=list)
    operations = models.JSONField(default=list)
    document_digest = models.CharField(max_length=64)
    publisher = models.ForeignKey(User, on_delete=models.PROTECT, related_name="published_registrar_agreements")
    published_at = models.DateTimeField()
    submission_key = models.UUIDField(unique=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["agreement_key", "version"], name="unique_registrar_agreement_version")]
        ordering = ["-published_at", "-pk"]


class RegistrarAgreementBinding(TimeStampedModel):
    agreement_key = models.SlugField(max_length=80)
    profile_key = models.CharField(max_length=80)
    operation = models.CharField(max_length=16)
    document = models.ForeignKey(RegistrarAgreementVersion, on_delete=models.PROTECT, related_name="current_bindings")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["agreement_key", "profile_key", "operation"], name="unique_registrar_agreement_scope")]


class RegistrarAgreementCatalogue(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    revision = models.PositiveBigIntegerField(default=0)


class DomainAgreementAcceptance(ImmutableAgreementRecord):
    key = models.UUIDField(unique=True)
    customer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="domain_agreement_acceptances")
    contact = models.ForeignKey("DomainContact", on_delete=models.PROTECT, related_name="agreement_acceptances")
    domain_name = models.CharField(max_length=255)
    operation = models.CharField(max_length=16)
    subject_digest = models.CharField(max_length=64)
    plan_digest = models.CharField(max_length=64)
    accepted_at = models.DateTimeField()


class DomainAgreementAcceptanceDocument(ImmutableAgreementRecord):
    acceptance = models.ForeignKey(DomainAgreementAcceptance, on_delete=models.PROTECT, related_name="documents")
    document = models.ForeignKey(RegistrarAgreementVersion, on_delete=models.PROTECT, related_name="acceptance_documents")
    document_digest = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["acceptance", "document"], name="unique_domain_accepted_document")]


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
    registrar_nameservers = models.JSONField(default=list, blank=True)
    epp_code = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "-created_at", "-id"], name="workspace_domain_created"),
            models.Index(fields=["user", "-updated_at", "-id"], name="workspace_domain_updated"),
            models.Index(SearchKey("name"), name="search_domain_name"),
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def cached_nameservers(self):
        """Complete cache when present; legacy fields remain an unverified fallback."""
        if self.registrar_nameservers:
            if (type(self.registrar_nameservers) is not list or len(self.registrar_nameservers) > 13
                    or any(type(value) is not str for value in self.registrar_nameservers)):
                from django.core.exceptions import ValidationError
                raise ValidationError("The saved nameserver cache requires review.")
            return list(self.registrar_nameservers)
        return [getattr(self, f"nameserver{index}") for index in range(1, 5) if getattr(self, f"nameserver{index}")]

    def set_nameserver_cache(self, nameservers):
        """Retain every supported hostname and mirror the legacy first four fields."""
        from .registry_requirements import validate_nameservers
        validate_nameservers(self.name, nameservers)
        values = [value.strip().lower().rstrip(".") for value in nameservers]
        self.registrar_nameservers = values
        for index in range(1, 5):
            setattr(self, f"nameserver{index}", values[index - 1] if len(values) >= index else "")

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


class VerificationQueueState(models.Model):
    """Singleton transaction lock for bounded registrar verification admission."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    scan_after = models.PositiveBigIntegerField(default=0)


class VerificationAction(TimeStampedModel):
    domain_order = models.ForeignKey("domains.DomainOrder", on_delete=models.CASCADE, related_name="verification_actions")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    is_automatic = models.BooleanField(default=False)
    available_at = models.DateTimeField(default=timezone.now)
    key = models.UUIDField(unique=True)
    kind = models.CharField(max_length=10, choices=[("refresh", "Refresh status"), ("resend", "Resend verification email")])
    status = models.CharField(max_length=12, default="queued", choices=[
        ("queued", "Queued"), ("running", "Running"), ("succeeded", "Completed"),
        ("failed", "Failed"), ("unknown", "Needs provider review"), ("cancelled", "Cancelled"),
    ])
    subject_digest = models.CharField(max_length=64)
    remote_contact_id = models.CharField(max_length=19, blank=True)
    provider_scope = models.CharField(max_length=64, blank=True)
    registrar_order_id = models.CharField(max_length=255)
    lease_until = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(
            fields=["domain_order", "kind"], condition=models.Q(status__in=["queued", "running", "unknown"]),
            name="one_open_verification_action",
        )]
        indexes = [models.Index(fields=["status", "created_at"], name="verification_action_queue")]


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
    registrant_type = models.CharField(
        max_length=20, default="unspecified",
        choices=[("unspecified", "Not confirmed"), ("individual", "Individual"),
                 ("organisation", "Organisation or sole trader")],
    )
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
    us_nexus_category = models.CharField(max_length=3, blank=True)
    us_application_purpose = models.CharField(max_length=2, blank=True)
    us_nexus_certified = models.BooleanField(default=False)
    eu_eligibility_basis = models.CharField(max_length=12, blank=True)
    eu_citizenship_country = models.CharField(max_length=2, blank=True)
    eu_eligibility_certified = models.BooleanField(default=False)
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
        from apps.domains.registrar_policy import contact_payload_policy
        return contact_payload_policy(tld).contact_type

    def as_resellerclub_payload(self, customer_id: str, tld: str = "") -> dict:
        """Build a contact payload without inventing or truncating registrant data.

        This validates general API field constraints, not identity, email ownership,
        or registry eligibility. TLD-specific policy checks remain separate.
        """
        from apps.domains.contact_validation import resellerclub_contact_payload

        return resellerclub_contact_payload(self, customer_id, tld)



class ContactVerification(TimeStampedModel):
    """Evidence for one check against one immutable contact-data snapshot.

    Existing local validation flags intentionally do not populate this table.
    A changed contact makes prior evidence stale without deleting its history.
    """

    CHECK_CHOICES = [
        ("identity", "Local identity review"),
        ("business", "Business record check"),
        ("registrar_email", "Registrar email verification"),
        ("eligibility", "TLD eligibility"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"), ("verified", "Verified"),
        ("rejected", "Rejected"), ("unknown", "Unknown"),
        ("suspended", "Suspended by registrar"),
    ]
    contact = models.ForeignKey(
        DomainContact, on_delete=models.CASCADE, related_name="verification_evidence"
    )
    check_kind = models.CharField(max_length=20, choices=CHECK_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="unknown")
    subject_digest = models.CharField(max_length=64, editable=False)
    remote_contact_id = models.CharField(max_length=19, blank=True)
    provider_scope = models.CharField(max_length=64, blank=True)
    source = models.CharField(max_length=100)
    reference = models.CharField(max_length=255, blank=True)
    tld = models.CharField(max_length=63, blank=True)
    checked_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True, blank=True)
    provider_started_at = models.DateTimeField(null=True, blank=True)
    action_due_at = models.DateTimeField(null=True, blank=True)
    domain_order = models.ForeignKey(
        "domains.DomainOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="verification_evidence",
    )
    domain_name = models.CharField(max_length=255, blank=True)

    review_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    reviewer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="contact_reviews")
    policy_reference = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-checked_at", "-pk"]
        indexes = [models.Index(fields=["contact", "check_kind", "tld", "-checked_at"], name="contact_check_evidence")]

    @staticmethod
    def digest_for(contact):
        import hashlib
        import json

        fields = (
            "user_id", "registrant_type", "name", "company", "company_number", "email",
            "phone_country_code", "phone", "address_line1", "address_line2",
            "city", "state", "postcode", "country", "registrar_contact_id",
        )
        values = {field: getattr(contact, field) for field in fields}
        # Blank new declarations do not change the historical digest contract.
        if contact.us_nexus_category or contact.us_application_purpose or contact.us_nexus_certified:
            values.update(us_nexus_category=contact.us_nexus_category,
                          us_application_purpose=contact.us_application_purpose,
                          us_nexus_certified=contact.us_nexus_certified)
        if contact.eu_eligibility_basis or contact.eu_citizenship_country or contact.eu_eligibility_certified:
            values.update(eu_eligibility_basis=contact.eu_eligibility_basis,
                          eu_citizenship_country=contact.eu_citizenship_country,
                          eu_eligibility_certified=contact.eu_eligibility_certified)
        serialized = json.dumps(
            values,
            sort_keys=True, ensure_ascii=True, separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @property
    def is_current(self):
        from django.utils import timezone

        if self.pk is None:
            return False
        # Re-read persisted data: a cached related object cannot establish freshness.
        current = DomainContact.objects.filter(pk=self.contact_id).first()
        if current is None:
            return False
        now = timezone.now()
        if self.source == "resellerclub" and self.check_kind == "registrar_email":
            if not self.domain_order_id:
                return False
            linked_order = DomainOrder.objects.filter(
                pk=self.domain_order_id, registrar_order_id=self.reference,
                registration_contact_id=self.contact_id, user_id=current.user_id,
                domain_name=self.domain_name, tld=self.tld,
            ).first()
            if linked_order is None:
                return False
            if self.remote_contact_id and linked_order.remote_registrant_id != self.remote_contact_id:
                return False
            if self.provider_scope != linked_order.registrar_contact_ids.get("_scope", ""):
                return False
        scope = {"contact_id": self.contact_id, "check_kind": self.check_kind,
                 "tld": self.tld, "domain_name": self.domain_name}
        if self.check_kind == "registrar_email":
            # Email observations belong to a particular provider order/account.
            scope.update(domain_order_id=self.domain_order_id, reference=self.reference,
                         provider_scope=self.provider_scope)
        elif self.check_kind == "eligibility":
            scope["domain_order_id"] = self.domain_order_id
        # Case references identify evidence, not independent approvals. A later
        # outcome for the same assessment scope supersedes every earlier case.
        return (
            self.subject_digest == self.digest_for(current)
            and self.checked_at <= now
            and (self.expires_at is None or self.expires_at > now)
            and type(self).objects.filter(**scope).values_list("pk", flat=True).first() == self.pk
        )

    @property
    def is_verified(self):
        return self.status == "verified" and self.is_current


class RegistrarContactBinding(TimeStampedModel):
    """Immutable provider contact creation reservation; never store raw contact data."""

    contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="registrar_bindings")
    customer_id = models.CharField(max_length=19)
    provider_scope = models.CharField(max_length=64)
    contact_type = models.CharField(max_length=32)
    payload_digest = models.CharField(max_length=64)
    remote_id = models.CharField(max_length=19, blank=True)
    status = models.CharField(max_length=16, default="creating", choices=[
        ("creating", "Creating"), ("ready", "Ready"), ("unknown", "Requires reconciliation"),
    ])

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["contact", "provider_scope", "customer_id", "contact_type", "payload_digest"],
            name="unique_registrar_contact_payload",
        )]


class RegistrantPublicationDecision(TimeStampedModel):
    """Explicit per-request publication choice, bound to a contact version."""

    customer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="registrant_publication_decisions")
    contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="publication_decisions")
    domain_name = models.CharField(max_length=255)
    choice = models.CharField(max_length=8, choices=[("redact", "Keep redacted"), ("publish", "Publish organization")])
    subject_digest = models.CharField(max_length=64)
    policy_version = models.CharField(max_length=32)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawal_ticket = models.ForeignKey("support.SupportTicket", on_delete=models.SET_NULL, null=True, blank=True)


class PublicationRedaction(TimeStampedModel):
    """One durable provider-redaction outcome for one withdrawn decision."""

    decision = models.OneToOneField(RegistrantPublicationDecision, on_delete=models.PROTECT, related_name="redaction")
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    status = models.CharField(max_length=16, default="queued", choices=[("queued", "Queued"),
        ("submitting", "Submission started"), ("awaiting", "Awaiting provider confirmation"),
        ("confirmed", "Provider consent revoked"), ("unknown", "Provider review required"),
        ("review", "Association review required")])
    provider_scope = models.CharField(max_length=64, blank=True)
    registrar_order_id = models.CharField(max_length=19, blank=True)
    customer_id = models.CharField(max_length=19, blank=True)
    role_ids = models.JSONField(default=dict)
    action_id = models.CharField(max_length=19, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    available_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "available_at"], name="publication_redact_dispatch")]


class ContactChangeQueueState(models.Model):
    """Singleton lock serializing global contact-change queue admission."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)


class DomainContactChange(TimeStampedModel):
    """Proposed domain associations remain separate until provider confirmation."""

    domain = models.ForeignKey(Domain, on_delete=models.PROTECT, related_name="contact_changes")
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    key = models.UUIDField(unique=True)
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    status = models.CharField(max_length=16, default="queued", choices=[
        ("queued", "Queued"), ("preparing", "Preparing contacts"), ("submitting", "Submission started"),
        ("awaiting", "Awaiting registrar confirmation"), ("confirmed", "Confirmed by registrar"),
        ("failed", "Stopped before submission"), ("unknown", "Provider review required"), ("cancelled", "Cancelled"),
    ])
    registration_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, related_name="registrant_changes")
    admin_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, null=True, blank=True, related_name="admin_changes")
    tech_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, null=True, blank=True, related_name="tech_changes")
    billing_contact = models.ForeignKey(DomainContact, on_delete=models.PROTECT, null=True, blank=True, related_name="billing_changes")
    publication_decision = models.ForeignKey(RegistrantPublicationDecision, on_delete=models.PROTECT, null=True, blank=True)
    domain_name = models.CharField(max_length=255)
    registrar_order_id = models.CharField(max_length=19)
    provider_scope = models.CharField(max_length=64)
    customer_id = models.CharField(max_length=19)
    subject = models.JSONField(default=dict)
    role_ids = models.JSONField(default=dict)
    action_id = models.CharField(max_length=19, blank=True)
    irtp_status = models.CharField(max_length=20, blank=True)
    provider_observation = models.JSONField(default=dict)
    available_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["domain"], condition=models.Q(
            status__in=["queued", "preparing", "submitting", "awaiting", "unknown"]), name="one_open_domain_contact_change")]
        indexes = [models.Index(fields=["status", "available_at"], name="contact_change_dispatch")]


class DomainRegistrationAttempt(TimeStampedModel):
    """Durable paid-call admission. An uncertain request is never replayed."""

    order = models.OneToOneField("DomainOrder", on_delete=models.PROTECT, related_name="registration_attempt")
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    status = models.CharField(max_length=16, default="preparing", choices=[
        ("preparing", "Preparing contacts"), ("submitting", "Submission started"),
        ("accepted", "Registrar order acknowledged"), ("unknown", "Requires registrar review"),
        ("aborted", "Stopped before paid submission"),
    ])
    provider_scope = models.CharField(max_length=64)
    customer_id = models.CharField(max_length=19)
    subject = models.JSONField(default=dict)
    role_ids = models.JSONField(default=dict)
    remote_order_id = models.CharField(max_length=19, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    legacy_review_required = models.BooleanField(default=False)
    registry_status = models.CharField(max_length=16, blank=True)
    registry_checked_at = models.DateTimeField(null=True, blank=True)
    registry_registered_at = models.DateField(null=True, blank=True)
    registry_expires_at = models.DateField(null=True, blank=True)


class DomainOrder(TimeStampedModel):
    agreement_acceptance = models.ForeignKey("domains.DomainAgreementAcceptance", on_delete=models.PROTECT, null=True, blank=True)
    publication_decision = models.ForeignKey(RegistrantPublicationDecision, on_delete=models.PROTECT, null=True, blank=True)
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
    registrar_contact_ids = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Domain order"
        verbose_name_plural = "Domain orders"
        indexes = [models.Index(fields=["status", "tld", "id"], name="verification_scan_order")]

    @property
    def remote_registrant_id(self):
        # Never fall back when a new order has a snapshot but its registrant is absent.
        if self.registrar_contact_ids:
            return self.registrar_contact_ids.get("reg", "")
        return self.registration_contact.registrar_contact_id

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


class DomainTransferAttempt(TimeStampedModel):
    """Retained paid transfer admission and worker fence; never stores auth codes."""

    transfer = models.OneToOneField("DomainTransfer", on_delete=models.PROTECT, related_name="submission_attempt")
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    status = models.CharField(max_length=16, default="preparing", choices=[
        ("preparing", "Preparing contacts"), ("submitting", "Submission started"),
        ("accepted", "Transfer order acknowledged"), ("unknown", "Requires registrar review"),
        ("aborted", "Stopped before paid submission"),
    ])
    provider_scope = models.CharField(max_length=64, blank=True)
    customer_id = models.CharField(max_length=19, blank=True)
    subject = models.JSONField(default=dict)
    role_ids = models.JSONField(default=dict)
    remote_order_id = models.CharField(max_length=19, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    legacy_review_required = models.BooleanField(default=False)


class BulkDomainBatch(TimeStampedModel):
    """Retained preview/confirmation identity; never infer consent from an old job."""

    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="bulk_domain_batches")
    action = models.CharField(max_length=16)
    target = models.JSONField()
    request_digest = models.CharField(max_length=64)
    manifest_digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    retry_of = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="retry_batches")
    retry_item_ids = models.JSONField(default=list, blank=True)
    retry_source_digest = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [models.Index(fields=("user", "-created_at", "-id"), name="bulk_batch_owner_created")]

    def save(self, *args, **kwargs):
        if self.pk:
            fields = ("key", "user_id", "action", "target", "request_digest", "manifest_digest", "expires_at", "retry_of_id", "retry_item_ids", "retry_source_digest")
            previous = type(self).objects.filter(pk=self.pk).values(*fields, "confirmed_at").first()
            if previous and any(previous[field] != getattr(self, field) for field in fields):
                from django.core.exceptions import ValidationError
                raise ValidationError("Retain the bulk preview; create a new request for changed settings.")
            if previous and previous["confirmed_at"] is not None and previous["confirmed_at"] != self.confirmed_at:
                from django.core.exceptions import ValidationError
                raise ValidationError("Retain the original bulk confirmation.")
        super().save(*args, **kwargs)


class BulkDomainItem(TimeStampedModel):
    batch = models.ForeignKey(BulkDomainBatch, on_delete=models.PROTECT, related_name="items")
    selected_id = models.PositiveBigIntegerField()
    domain = models.ForeignKey(Domain, on_delete=models.PROTECT, null=True, blank=True, related_name="bulk_operation_items")
    name = models.CharField(max_length=255)
    preview_status = models.CharField(max_length=16)
    snapshot = models.JSONField(default=dict)
    preview_message = models.CharField(max_length=2000)
    provider_action_id = models.CharField(max_length=20, blank=True)
    observed_lock = models.BooleanField(null=True, blank=True)
    observed_nameservers = models.JSONField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    registrar_completed_at = models.DateTimeField(null=True, blank=True)
    hosting_status = models.CharField(max_length=16, blank=True)
    status = models.CharField(max_length=16)
    message = models.CharField(max_length=2000, blank=True)
    contact_change = models.OneToOneField("DomainContactChange", on_delete=models.PROTECT, null=True, blank=True, related_name="bulk_item")
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    available_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True, blank=True)
    dispatch_until = models.DateTimeField(null=True, blank=True)
    dispatch_token = models.UUIDField(null=True, blank=True, editable=False)
    dispatch_after = models.DateTimeField(default=timezone.now)
    paused_at = models.DateTimeField(null=True, blank=True)
    pause_token = models.UUIDField(null=True, blank=True, editable=False)
    paused_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name="paused_bulk_domain_items")
    token = models.UUIDField(default=uuid.uuid4, editable=False)

    class Meta:
        ordering = ("pk",)
        constraints = [models.UniqueConstraint(fields=("batch", "selected_id"), name="bulk_item_selected_once")]
        indexes = [models.Index(fields=("status", "available_at"), name="bulk_item_dispatch")]

    def save(self, *args, **kwargs):
        if self.pk:
            fields = ("batch_id", "selected_id", "domain_id", "name", "preview_status", "snapshot", "preview_message")
            previous = type(self).objects.filter(pk=self.pk).values(*fields).first()
            if previous and any(previous[field] != getattr(self, field) for field in fields):
                from django.core.exceptions import ValidationError
                raise ValidationError("Bulk preview history cannot be rewritten.")
        super().save(*args, **kwargs)


class DomainOperationReservation(TimeStampedModel):
    """An occupied domain slot has no automatic expiry after uncertain work."""

    domain = models.OneToOneField(Domain, on_delete=models.PROTECT, related_name="operation_reservation")
    item = models.OneToOneField(BulkDomainItem, on_delete=models.PROTECT, related_name="reservation")


class DomainDNSChange(TimeStampedModel):
    """Retained provider-switch intent; submission markers never imply failure."""
    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    domain = models.ForeignKey(Domain, on_delete=models.PROTECT, related_name="dns_changes")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="dns_changes")
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="requested_dns_changes")
    target_provider = models.CharField(max_length=16)
    provider_scope = models.CharField(max_length=64)
    source = models.JSONField(default=dict)
    request_digest = models.CharField(max_length=64)
    manifest_digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, default="draft")
    zone_submitted_at = models.DateTimeField(null=True, blank=True)
    zone_id = models.CharField(max_length=255, blank=True)
    zone_observation = models.JSONField(default=dict, blank=True)
    inventory_receipt = models.JSONField(default=dict, blank=True)
    inventory_digest = models.CharField(max_length=64, blank=True)
    inventory_observed_at = models.DateTimeField(null=True, blank=True)
    inventory_token = models.UUIDField(null=True, blank=True, editable=False)
    inventory_lease_until = models.DateTimeField(null=True, blank=True)
    inventory_message = models.CharField(max_length=500, blank=True)
    approved_record_plan = models.ForeignKey("DomainDNSRecordPlan", on_delete=models.PROTECT, null=True, blank=True, related_name="approved_changes")
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name="cancelled_dns_changes")
    cancellation_reason = models.CharField(max_length=32, blank=True)

    def save(self, *args, **kwargs):
        if self.pk:
            fields = ("key", "domain_id", "user_id", "requested_by_id", "target_provider", "provider_scope",
                      "source", "request_digest", "manifest_digest", "expires_at")
            prior = type(self).objects.filter(pk=self.pk).values(*fields, "confirmed_at", "zone_submitted_at", "zone_id",
                "inventory_digest", "inventory_receipt", "inventory_observed_at", "approved_record_plan_id",
                "status", "cancelled_at", "cancelled_by_id", "cancellation_reason").first()
            from django.core.exceptions import ValidationError
            if prior and any(prior[field] != getattr(self, field) for field in fields):
                raise ValidationError("Retain the DNS switch preview. Review a new request for changed choices.")
            if prior and prior["confirmed_at"] is not None and prior["confirmed_at"] != self.confirmed_at:
                raise ValidationError("Retain the original DNS switch confirmation.")
            if prior and prior["zone_submitted_at"] is not None and prior["zone_submitted_at"] != self.zone_submitted_at:
                raise ValidationError("Retain the DNS provider submission marker for reconciliation.")
            if prior and prior["zone_id"] and prior["zone_id"] != self.zone_id:
                raise ValidationError("Retain the DNS provider zone association for reconciliation.")
            if prior and prior["inventory_digest"] and any(prior[field] != getattr(self, field)
                    for field in ("inventory_digest", "inventory_receipt", "inventory_observed_at")):
                raise ValidationError("Retain the DNS inventory receipt. Review a new request for changed evidence.")
            if prior and prior["approved_record_plan_id"] is not None and prior["approved_record_plan_id"] != self.approved_record_plan_id:
                raise ValidationError("Retain the approved DNS record plan association.")
            if prior and prior["cancelled_at"] is not None and any(prior[field] != getattr(self, field)
                    for field in ("status", "cancelled_at", "cancelled_by_id", "cancellation_reason")):
                raise ValidationError("Retain the cancelled DNS request and its original attribution.")
        if self.approved_record_plan_id is not None and not DomainDNSRecordPlan.objects.filter(
                pk=self.approved_record_plan_id, change_id=self.pk, approved_at__isnull=False, approved_by__isnull=False).exists():
            from django.core.exceptions import ValidationError
            raise ValidationError("Choose an approved record plan belonging to this DNS request.")
        super().save(*args, **kwargs)


class DomainDNSReservation(TimeStampedModel):
    domain = models.OneToOneField(Domain, on_delete=models.PROTECT, related_name="dns_reservation")
    change = models.OneToOneField(DomainDNSChange, on_delete=models.PROTECT, related_name="reservation")


class DomainDNSRecordPlan(TimeStampedModel):
    change = models.ForeignKey(DomainDNSChange, on_delete=models.PROTECT, related_name="record_plans")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_dns_record_plans")
    plan = models.JSONField(default=dict)
    digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name="approved_dns_record_plans")

    class Meta:
        constraints = [models.UniqueConstraint(fields=("change", "digest"), name="dns_record_plan_digest_once")]

    def save(self, *args, **kwargs):
        if self.pk:
            fields = ("change_id", "created_by_id", "plan", "digest", "expires_at")
            previous = type(self).objects.filter(pk=self.pk).values(*fields, "approved_at", "approved_by_id").first()
            from django.core.exceptions import ValidationError
            if previous and any(previous[field] != getattr(self, field) for field in fields):
                raise ValidationError("Retain the DNS record plan. Review a new revision for changed choices.")
            if previous and previous["approved_at"] is not None and (previous["approved_at"] != self.approved_at or previous["approved_by_id"] != self.approved_by_id):
                raise ValidationError("Retain the original DNS record plan approval.")
        super().save(*args, **kwargs)


class DomainTransfer(TimeStampedModel):
    agreement_acceptance = models.ForeignKey("domains.DomainAgreementAcceptance", on_delete=models.PROTECT, null=True, blank=True)
    publication_decision = models.ForeignKey(RegistrantPublicationDecision, on_delete=models.PROTECT, null=True, blank=True)
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
    registrar_contact_ids = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Domain transfer"
        verbose_name_plural = "Domain transfers"

    def __str__(self):
        return f"Transfer: {self.domain_name} ({self.status})"
