from django.db import models
from django.core.exceptions import ValidationError
from apps.core.models import TimeStampedModel
from apps.accounts.models import User

# 5 MB hard limit for support attachments
_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

# Permitted MIME-type-mapped extensions (checked against the file name)
_ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".log",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".zip", ".tar", ".gz",
    ".doc", ".docx", ".xls", ".xlsx", ".csv",
}


def _validate_attachment(upload):
    """Validate the size and extension of a support ticket attachment."""
    import os

    # Size check
    if upload.size > _MAX_ATTACHMENT_BYTES:
        raise ValidationError(
            f"Attachment must be smaller than {_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB. "
            f"Your file is {upload.size // (1024 * 1024)} MB."
        )

    # Extension allow-list check
    _, ext = os.path.splitext(upload.name.lower())
    if ext not in _ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"File type '{ext}' is not permitted. "
            f"Allowed types: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
        )


class Department(TimeStampedModel):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class SupportTicket(TimeStampedModel):
    STATUS_OPEN = "open"
    STATUS_AWAITING_CLIENT = "awaiting_client"
    STATUS_AWAITING_SUPPORT = "awaiting_support"
    STATUS_ON_HOLD = "on_hold"
    STATUS_RESOLVED = "resolved"
    STATUS_CLOSED = "closed"

    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_AWAITING_CLIENT, "Awaiting Client"),
        (STATUS_AWAITING_SUPPORT, "Awaiting Support"),
        (STATUS_ON_HOLD, "On Hold"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_CLOSED, "Closed"),
    ]

    PRIORITY_LOW = "low"
    PRIORITY_NORMAL = "normal"
    PRIORITY_HIGH = "high"
    PRIORITY_URGENT = "urgent"

    PRIORITY_CHOICES = [
        (PRIORITY_LOW, "Low"),
        (PRIORITY_NORMAL, "Normal"),
        (PRIORITY_HIGH, "High"),
        (PRIORITY_URGENT, "Urgent"),
    ]

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="support_tickets")
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True)
    subject = models.CharField(max_length=255)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_OPEN)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_tickets"
    )
    related_service = models.ForeignKey(
        "services.Service", on_delete=models.SET_NULL, null=True, blank=True
    )
    related_domain = models.ForeignKey(
        "domains.Domain", on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"#{self.id}: {self.subject}"


class SupportTicketMessage(TimeStampedModel):
    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    content = models.TextField()
    is_internal = models.BooleanField(default=False)
    attachment = models.FileField(
        upload_to="support/attachments/%Y/%m/",
        null=True,
        blank=True,
        validators=[_validate_attachment],
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Message on ticket #{self.ticket.id}"
