"""Django email backend that sends via Microsoft Graph when enabled."""
import logging

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail import get_connection

from apps.notifications.m365_graph import (
    MicrosoftGraphMailClient,
    MicrosoftGraphMailConfig,
    MicrosoftGraphMailError,
    address_list,
    attachment_payload,
)

logger = logging.getLogger(__name__)


class MicrosoftGraphEmailBackend(BaseEmailBackend):
    """Send Django EmailMessage objects using Microsoft Graph sendMail."""

    def __init__(self, *args, **kwargs):
        self._fallback_connection = None
        super().__init__(*args, **kwargs)

    def _fallback(self):
        if self._fallback_connection is None:
            backend = getattr(
                settings,
                "M365_GRAPH_FALLBACK_EMAIL_BACKEND",
                "django.core.mail.backends.console.EmailBackend",
            )
            self._fallback_connection = get_connection(backend=backend, fail_silently=self.fail_silently)
        return self._fallback_connection

    def send_messages(self, email_messages):
        config = MicrosoftGraphMailConfig.load()
        if not config.enabled:
            return self._fallback().send_messages(email_messages)

        sent = 0
        client = MicrosoftGraphMailClient(config)
        for message in email_messages or []:
            try:
                purpose = self._purpose_for(message)
                mailbox = config.mailbox_for(purpose)
                config.validate(purpose=purpose)
                client.send_mail(mailbox=mailbox, message=self._graph_message(message))
                sent += 1
            except Exception as exc:
                if self.fail_silently:
                    logger.warning("Microsoft Graph mail send failed silently: %s", exc)
                    continue
                if isinstance(exc, MicrosoftGraphMailError):
                    raise
                raise MicrosoftGraphMailError(str(exc)) from exc
        return sent

    def _purpose_for(self, message) -> str:
        headers = getattr(message, "extra_headers", {}) or {}
        purpose = headers.get("X-CyberAsk-Mailbox-Purpose") or headers.get("X-Mailbox-Purpose")
        if purpose:
            return str(purpose).strip().lower()
        subject = (getattr(message, "subject", "") or "").lower()
        if any(term in subject for term in ("invoice", "payment", "quote")):
            return "billing"
        if any(term in subject for term in ("ticket", "support")):
            return "support"
        if "domain" in subject:
            return "domains"
        return "notifications"

    def _graph_message(self, message) -> dict:
        html_body = None
        for content, mimetype in getattr(message, "alternatives", []) or []:
            if mimetype == "text/html":
                html_body = content
                break

        body_content = html_body or getattr(message, "body", "") or ""
        body_type = "HTML" if html_body else "Text"
        graph_message = {
            "subject": message.subject,
            "body": {"contentType": body_type, "content": body_content},
            "toRecipients": address_list(message.to),
        }
        if getattr(message, "cc", None):
            graph_message["ccRecipients"] = address_list(message.cc)
        if getattr(message, "bcc", None):
            graph_message["bccRecipients"] = address_list(message.bcc)
        if getattr(message, "reply_to", None):
            graph_message["replyTo"] = address_list(message.reply_to)

        attachments = []
        for attachment in getattr(message, "attachments", []) or []:
            if hasattr(attachment, "get_filename"):
                payload = attachment_payload(
                    attachment.get_filename() or "attachment",
                    attachment.get_payload(decode=True),
                    attachment.get_content_type(),
                )
            else:
                filename, content, mimetype = attachment
                payload = attachment_payload(filename, content, mimetype)
            attachments.append(payload)
        if attachments:
            graph_message["attachments"] = attachments

        return graph_message
