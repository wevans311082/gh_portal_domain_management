from django.db import models
from django.conf import settings
from django.utils.text import slugify


class WebsiteTemplate(models.Model):
    """
    Represents one extractable website template available in the gallery.
    A template is a complete static HTML site extracted from a ZIP archive.
    """

    CATEGORY_CHOICES = [
        ("hosting", "Hosting / Tech"),
        ("business", "Business / Corporate"),
        ("portfolio", "Portfolio / CV"),
        ("restaurant", "Restaurant / Food"),
        ("wedding", "Wedding / Events"),
        ("photography", "Photography"),
        ("ecommerce", "E-Commerce / Shop"),
        ("construction", "Under Construction"),
        ("blog", "Blog / Magazine"),
        ("other", "Other"),
    ]

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=130, unique=True)
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default="other")
    description = models.TextField(blank=True)
    zip_filename = models.CharField(max_length=200, blank=True, help_text="Original ZIP file name")
    extracted_path = models.CharField(
        max_length=300,
        blank=True,
        help_text="Relative path inside WEBSITE_TEMPLATES_EXTRACTED_ROOT",
    )
    has_index = models.BooleanField(default=True, help_text="Has a discoverable index.html")

    # Security / quality audit
    security_notes = models.TextField(
        blank=True,
        help_text="Auto-generated notes from security scan (old JS libs, http:// CDN, etc.)",
    )
    jquery_version = models.CharField(max_length=20, blank=True)
    bootstrap_version = models.CharField(max_length=20, blank=True)
    is_sanitised = models.BooleanField(
        default=False,
        help_text="Set to True after the security scan has patched this template",
    )

    # Availability
    is_active = models.BooleanField(default=True, help_text="Show in gallery")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "name"]
        verbose_name = "Website Template"
        verbose_name_plural = "Website Templates"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def preview_url(self):
        """URL that serves the template's index.html via Django."""
        return f"/website-templates/preview/{self.slug}/"

    @property
    def gallery_thumbnail_url(self):
        """Return a static thumbnail if it exists, otherwise a placeholder."""
        return f"/website-templates/thumbnail/{self.slug}/"


class TemplateInstallation(models.Model):
    """
    Tracks which website template a customer has installed for a given service.
    """

    STATUS_CHOICES = [
        ("active", "Active"),
        ("removed", "Removed"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="template_installations",
    )
    template = models.ForeignKey(
        WebsiteTemplate,
        on_delete=models.PROTECT,
        related_name="installations",
    )
    # Optionally link to a hosting service
    service_domain = models.CharField(
        max_length=253,
        blank=True,
        help_text="The hosting domain this template was installed for",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    installed_at = models.DateTimeField(auto_now_add=True)
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-installed_at"]
        verbose_name = "Template Installation"

    def __str__(self):
        return f"{self.user} → {self.template} ({self.service_domain or 'no domain'})"
