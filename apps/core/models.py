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
