from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SiteContentSettings(TimeStampedModel):
    site_tagline = models.CharField(max_length=255, blank=True)
    footer_about = models.TextField(blank=True)
    support_email = models.EmailField(blank=True)
    support_phone = models.CharField(max_length=50, blank=True)
    enable_cookie_banner = models.BooleanField(default=True)
    cookie_banner_text = models.CharField(
        max_length=255,
        default="We use cookies to improve your experience. You can accept or reject non-essential cookies.",
    )
    cookie_policy_slug = models.SlugField(default="cookie-policy")

    class Meta:
        verbose_name = "Site content settings"
        verbose_name_plural = "Site content settings"

    def __str__(self):
        return "Site content settings"

    @classmethod
    def get_solo(cls):
        obj = cls.objects.order_by("id").first()
        if obj:
            return obj
        return cls.objects.create()


class HomeFAQ(TimeStampedModel):
    question = models.CharField(max_length=255)
    answer = models.TextField()
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Home FAQ"
        verbose_name_plural = "Home FAQs"

    def __str__(self):
        return self.question


class HomeServiceCard(TimeStampedModel):
    title = models.CharField(max_length=120)
    subtitle = models.CharField(max_length=180, blank=True)
    description = models.TextField(blank=True)
    icon_emoji = models.CharField(max_length=8, default="⚡")
    cta_label = models.CharField(max_length=60, default="Learn more")
    cta_url = models.CharField(max_length=255, blank=True)
    features = models.TextField(blank=True, help_text="One feature per line")
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Home service card"
        verbose_name_plural = "Home service cards"

    def __str__(self):
        return self.title


class LegalPage(TimeStampedModel):
    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=200)
    summary = models.CharField(max_length=255, blank=True)
    content = models.TextField()
    show_in_footer = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "title"]
        verbose_name = "Legal page"
        verbose_name_plural = "Legal pages"

    def __str__(self):
        return self.title


class ContactFormSettings(TimeStampedModel):
    """Configures the public Contact Us form destination behaviour."""

    DESTINATION_DB = "db"
    DESTINATION_EMAIL = "email"
    DESTINATION_BOTH = "both"
    DESTINATION_CHOICES = [
        (DESTINATION_DB, "Store in database (admin review table)"),
        (DESTINATION_EMAIL, "Send by email only"),
        (DESTINATION_BOTH, "Both — store in DB and send email"),
    ]

    destination = models.CharField(
        max_length=10,
        choices=DESTINATION_CHOICES,
        default=DESTINATION_DB,
        help_text="Where submitted contact forms are delivered.",
    )
    destination_email = models.EmailField(
        blank=True,
        help_text="Required when destination includes email.",
    )
    notify_on_submit = models.BooleanField(
        default=True,
        help_text="Send a notification email to destination_email for each submission.",
    )
    form_title = models.CharField(
        max_length=120,
        default="Contact Us",
        help_text="Heading shown on the public contact page.",
    )
    form_intro = models.TextField(
        blank=True,
        default="Fill in the form below and we'll get back to you as soon as possible.",
        help_text="Introductory text above the form.",
    )
    thank_you_message = models.CharField(
        max_length=255,
        default="Thank you for getting in touch. We'll be in touch shortly.",
        help_text="Success message shown after form submission.",
    )

    class Meta:
        verbose_name = "Contact form settings"
        verbose_name_plural = "Contact form settings"

    def __str__(self):
        return "Contact form settings"

    @classmethod
    def get_solo(cls):
        obj = cls.objects.order_by("id").first()
        if obj:
            return obj
        return cls.objects.create()


class ContactSubmission(TimeStampedModel):
    """A submitted contact form entry stored for admin review."""

    STATUS_NEW = "new"
    STATUS_READ = "read"
    STATUS_REPLIED = "replied"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_READ, "Read"),
        (STATUS_REPLIED, "Replied"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=50, blank=True)
    subject = models.CharField(max_length=255, blank=True)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    admin_notes = models.TextField(blank=True, help_text="Staff-only notes on this enquiry.")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Contact submission"
        verbose_name_plural = "Contact submissions"

    def __str__(self):
        return f"Contact from {self.name} <{self.email}> ({self.created_at:%Y-%m-%d})"


class ErrorPageContent(TimeStampedModel):
    STATUS_404 = "404"
    STATUS_500 = "500"
    STATUS_CHOICES = [(STATUS_404, "404 Not Found"), (STATUS_500, "500 Server Error")]

    status_code = models.CharField(max_length=3, choices=STATUS_CHOICES, unique=True)
    title = models.CharField(max_length=200)
    subtitle = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    cta_label = models.CharField(max_length=60, default="Back to home")
    cta_url = models.CharField(max_length=255, default="/")
    animation_style = models.CharField(max_length=50, default="float-orbs")

    class Meta:
        verbose_name = "Error page content"
        verbose_name_plural = "Error page content"

    def __str__(self):
        return f"{self.status_code} page"


# ---------------------------------------------------------------------------
# Phase 5 CMS: Blog posts and testimonials
# ---------------------------------------------------------------------------

class BlogPost(TimeStampedModel):
    STATUS_DRAFT = "draft"
    STATUS_PUBLISHED = "published"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PUBLISHED, "Published"),
    ]

    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, max_length=255)
    excerpt = models.TextField(blank=True, help_text="Short summary shown in listings.")
    body = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    author = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="blog_posts",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    meta_description = models.CharField(max_length=160, blank=True, help_text="SEO meta description (max 160 chars).")
    featured_image_url = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]
        verbose_name = "Blog post"
        verbose_name_plural = "Blog posts"

    def __str__(self):
        return self.title

    def is_published(self):
        return self.status == self.STATUS_PUBLISHED


class Testimonial(TimeStampedModel):
    name = models.CharField(max_length=120)
    company = models.CharField(max_length=120, blank=True)
    body = models.TextField(help_text="The testimonial text.")
    rating = models.PositiveSmallIntegerField(default=5, help_text="1–5 stars")
    avatar_url = models.CharField(max_length=500, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "-created_at"]
        verbose_name = "Testimonial"
        verbose_name_plural = "Testimonials"

    def __str__(self):
        return f"{self.name} — {self.company}" if self.company else self.name


# ---------------------------------------------------------------------------
# Phase 7: Marketing
# ---------------------------------------------------------------------------

class PromoCode(TimeStampedModel):
    """Discount codes that can be applied at checkout."""

    DISCOUNT_PERCENT = "percent"
    DISCOUNT_FIXED = "fixed"
    DISCOUNT_CHOICES = [
        (DISCOUNT_PERCENT, "Percentage off"),
        (DISCOUNT_FIXED, "Fixed amount off"),
    ]

    code = models.CharField(max_length=50, unique=True)
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_CHOICES, default=DISCOUNT_PERCENT)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, help_text="Percentage (0–100) or fixed amount.")
    max_uses = models.PositiveIntegerField(null=True, blank=True, help_text="Leave blank for unlimited.")
    uses = models.PositiveIntegerField(default=0)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Promo code"
        verbose_name_plural = "Promo codes"

    def __str__(self):
        return self.code

    def is_valid(self):
        from django.utils import timezone
        now = timezone.now()
        if not self.is_active:
            return False
        if self.max_uses is not None and self.uses >= self.max_uses:
            return False
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_until and now > self.valid_until:
            return False
        return True

    def apply(self, amount):
        """Return the discounted amount after applying this code."""
        from decimal import Decimal, ROUND_HALF_UP
        if self.discount_type == self.DISCOUNT_PERCENT:
            discount = (amount * self.discount_value / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            discount = min(self.discount_value, amount)
        return max(Decimal("0.00"), amount - discount)


class AnnouncementBanner(TimeStampedModel):
    """Sitewide announcement banners shown to logged-in users."""

    LEVEL_INFO = "info"
    LEVEL_WARNING = "warning"
    LEVEL_DANGER = "danger"
    LEVEL_SUCCESS = "success"
    LEVEL_CHOICES = [
        (LEVEL_INFO, "Info (blue)"),
        (LEVEL_WARNING, "Warning (amber)"),
        (LEVEL_DANGER, "Danger (red)"),
        (LEVEL_SUCCESS, "Success (green)"),
    ]

    message = models.TextField()
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default=LEVEL_INFO)
    is_active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    show_to_staff_only = models.BooleanField(default=False)
    url = models.CharField(max_length=500, blank=True, help_text="Optional CTA link.")
    url_label = models.CharField(max_length=100, blank=True, help_text="CTA button label.")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Announcement banner"
        verbose_name_plural = "Announcement banners"

    def __str__(self):
        return self.message[:80]

    def is_visible(self):
        from django.utils import timezone
        now = timezone.now()
        if not self.is_active:
            return False
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_until and now > self.valid_until:
            return False
        return True


# ---------------------------------------------------------------------------
# Phase 9: API Keys (hashed, prefix-indexed)
# ---------------------------------------------------------------------------

class APIKey(TimeStampedModel):
    """Hashed API key for programmatic access.

    The full key is shown once at creation and never stored.
    Only the first 8 chars (prefix) and a SHA-256 hash are persisted.
    """

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="api_keys",
    )
    name = models.CharField(max_length=100, help_text="Friendly name for this key")
    key_prefix = models.CharField(max_length=8, db_index=True)
    key_hash = models.CharField(max_length=64, unique=True)  # SHA-256 hex
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.key_prefix}…)"

    @classmethod
    def generate(cls, user, name, expires_at=None):
        """Generate a new key, save the hash, and return (instance, raw_key)."""
        import hashlib
        import secrets
        raw_key = secrets.token_urlsafe(32)
        prefix = raw_key[:8]
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        instance = cls.objects.create(
            user=user,
            name=name,
            key_prefix=prefix,
            key_hash=key_hash,
            expires_at=expires_at,
        )
        return instance, raw_key

    @classmethod
    def authenticate(cls, raw_key):
        """Return the APIKey if the raw key is valid and active, else None."""
        import hashlib
        from django.utils import timezone as tz
        if not raw_key or len(raw_key) < 8:
            return None
        prefix = raw_key[:8]
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        try:
            key = cls.objects.get(key_prefix=prefix, key_hash=key_hash, is_active=True)
        except cls.DoesNotExist:
            return None
        if key.expires_at and key.expires_at < tz.now():
            return None
        key.last_used_at = tz.now()
        key.save(update_fields=["last_used_at"])
        return key


# ---------------------------------------------------------------------------
# Phase 9: SLA tracking
# ---------------------------------------------------------------------------

class SLAEvent(TimeStampedModel):
    """Track service-level events (outages, degradations, maintenance)."""

    SEV_CRITICAL = "critical"
    SEV_HIGH = "high"
    SEV_MEDIUM = "medium"
    SEV_LOW = "low"
    SEVERITY_CHOICES = [
        (SEV_CRITICAL, "Critical"),
        (SEV_HIGH, "High"),
        (SEV_MEDIUM, "Medium"),
        (SEV_LOW, "Low"),
    ]

    TYPE_OUTAGE = "outage"
    TYPE_DEGRADATION = "degradation"
    TYPE_MAINTENANCE = "maintenance"
    EVENT_TYPE_CHOICES = [
        (TYPE_OUTAGE, "Outage"),
        (TYPE_DEGRADATION, "Degradation"),
        (TYPE_MAINTENANCE, "Maintenance"),
    ]

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sla_events",
        help_text="Affected user, or blank for platform-wide event",
    )
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default=SEV_MEDIUM)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    started_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    is_resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"[{self.severity}] {self.title}"

    @property
    def duration_minutes(self):
        if self.resolved_at and self.started_at:
            return int((self.resolved_at - self.started_at).total_seconds() / 60)
        return None
