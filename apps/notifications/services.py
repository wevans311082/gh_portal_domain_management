"""Notification service for sending templated emails."""
import logging
import re
from typing import Iterable, Optional, Sequence, Tuple

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


# Attachment shape: ``(filename, content_bytes, content_type)``.
AttachmentTuple = Tuple[str, bytes, str]


NOTIFICATION_TEMPLATES = {
    "welcome": {
        "subject": "Welcome to {site_name}",
        "template": "emails/welcome.html",
    },
    "invoice_issued": {
        "subject": "Invoice #{invoice_number} from {site_name}",
        "template": "emails/invoice_issued.html",
    },
    "invoice_paid": {
        "subject": "Payment received - Invoice #{invoice_number}",
        "template": "emails/invoice_paid.html",
    },
    "invoice_overdue": {
        "subject": "Invoice #{invoice_number} is overdue",
        "template": "emails/invoice_overdue.html",
    },
    "quote_sent": {
        "subject": "Your quote #{quote_number} from {site_name}",
        "template": "emails/quote_sent.html",
    },
    "quote_accepted": {
        "subject": "Quote #{quote_number} accepted",
        "template": "emails/quote_accepted.html",
    },
    "quote_expired": {
        "subject": "Quote #{quote_number} has expired",
        "template": "emails/quote_expired.html",
    },
    "payment_failed": {
        "subject": "Payment failed - Action required",
        "template": "emails/payment_failed.html",
    },
    "hosting_provisioned": {
        "subject": "Your hosting account is ready - {domain}",
        "template": "emails/hosting_provisioned.html",
    },
    "hosting_suspended": {
        "subject": "Your hosting account has been suspended",
        "template": "emails/hosting_suspended.html",
    },
    "domain_expiry_reminder": {
        "subject": "Your domain {domain} is expiring soon",
        "template": "emails/domain_expiry_reminder.html",
    },
    "support_ticket_opened": {
        "subject": "Support ticket #{ticket_id} opened",
        "template": "emails/support_ticket_opened.html",
    },
    "support_ticket_reply": {
        "subject": "New reply on ticket #{ticket_id}",
        "template": "emails/support_ticket_reply.html",
    },
}


def send_notification(
    template_name: str,
    user,
    context: dict = None,
    *,
    attachments: Optional[Sequence[AttachmentTuple]] = None,
    recipient_email: Optional[str] = None,
    cc: Optional[Iterable[str]] = None,
):
    """Send a templated notification email to a user.

    Optional kwargs:
    - ``attachments``: iterable of ``(filename, bytes, content_type)`` tuples.
    - ``recipient_email``: explicit recipient (used for anonymous quotes).
    - ``cc``: optional iterable of CC addresses.
    """
    context = dict(context or {})
    context.setdefault("site_name", getattr(settings, "SITE_NAME", "Grumpy Hosting"))
    context.setdefault("user", user)

    template_config = NOTIFICATION_TEMPLATES.get(template_name)
    if not template_config:
        logger.warning("Unknown notification template: %s", template_name)
        return

    subject_template = template_config["subject"]
    try:
        subject = subject_template.format(**context)
    except (KeyError, AttributeError):
        subject = subject_template

    template_path = template_config["template"]
    recipient = recipient_email or getattr(user, "email", "") or ""

    if not recipient:
        logger.warning("send_notification: no recipient for %s", template_name)
        return

    try:
        html_content = render_to_string(template_path, context)
        text_content = re.sub(r"<[^>]+>", "", html_content).strip()

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
            cc=list(cc) if cc else None,
        )
        msg.attach_alternative(html_content, "text/html")

        for filename, content, content_type in attachments or []:
            msg.attach(filename, content, content_type)

        msg.send()

        try:
            from apps.audit.models import EmailLog

            EmailLog.objects.create(
                recipient=recipient,
                subject=subject,
                template=template_name,
                status="sent",
            )
        except Exception:  # pragma: no cover - audit is best-effort
            pass

        logger.info("Sent %s email to %s", template_name, recipient)
    except Exception as exc:
        logger.error("Failed to send %s email to %s: %s", template_name, recipient, exc)
        try:
            from apps.audit.models import EmailLog

            EmailLog.objects.create(
                recipient=recipient,
                subject=subject if "subject" in locals() else "",
                template=template_name,
                status="failed",
                error=str(exc),
            )
        except Exception:  # pragma: no cover
            pass
