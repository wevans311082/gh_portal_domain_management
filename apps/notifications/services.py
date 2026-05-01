import logging
from django.core.mail import send_mail
from django.template import Template, Context
from django.conf import settings
from .models import NotificationTemplate
from apps.audit.models import EmailLog

logger = logging.getLogger(__name__)


def send_notification(template_name, recipient_email, context_data=None):
    try:
        template = NotificationTemplate.objects.get(name=template_name, is_active=True)
    except NotificationTemplate.DoesNotExist:
        logger.error(f"Notification template '{template_name}' not found")
        return False

    ctx = Context(context_data or {})
    subject = Template(template.subject).render(ctx)
    html_message = Template(template.html_content).render(ctx)
    text_message = Template(template.text_content).render(ctx) if template.text_content else ""

    try:
        send_mail(
            subject=subject,
            message=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient_email],
            html_message=html_message,
        )
        EmailLog.objects.create(recipient=recipient_email, subject=subject, template=template_name)
        return True
    except Exception as e:
        logger.error(f"Failed to send notification '{template_name}' to {recipient_email}: {e}")
        EmailLog.objects.create(recipient=recipient_email, subject=subject, template=template_name, status="failed", error=str(e))
        return False
