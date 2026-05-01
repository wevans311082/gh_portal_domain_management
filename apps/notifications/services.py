"""Notification service for sending templated emails."""
import logging
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings

logger = logging.getLogger(__name__)

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


def send_notification(template_name: str, user, context: dict = None):
    """Send a templated notification email to a user."""
    context = context or {}
    context.setdefault("site_name", settings.SITE_NAME)
    context.setdefault("user", user)

    template_config = NOTIFICATION_TEMPLATES.get(template_name)
    if not template_config:
        logger.warning(f"Unknown notification template: {template_name}")
        return

    # Format subject safely - only replace keys that exist in context
    subject_template = template_config["subject"]
    try:
        subject = subject_template.format(**context)
    except (KeyError, AttributeError):
        subject = subject_template

    template_path = template_config["template"]

    try:
        html_content = render_to_string(template_path, context)
        # Simplified plain text - strip HTML tags
        import re
        text_content = re.sub(r"<[^>]+>", "", html_content).strip()

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send()

        from apps.audit.models import EmailLog
        EmailLog.objects.create(
            recipient=user.email,
            subject=subject,
            template=template_name,
            status="sent",
        )

        logger.info(f"Sent {template_name} email to {user.email}")
    except Exception as e:
        logger.error(f"Failed to send {template_name} email to {user.email}: {e}")
        from apps.audit.models import EmailLog
        EmailLog.objects.create(
            recipient=user.email,
            subject=subject,
            template=template_name,
            status="failed",
            error=str(e),
        )
