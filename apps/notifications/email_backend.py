"""Django email backend that sends via Microsoft Graph when enabled."""
import logging
import socket

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


def _smtp_host_usable() -> bool:
    """Return False when EMAIL_HOST is empty or DNS cannot resolve it."""
    host = (getattr(settings, "EMAIL_HOST", "") or "").strip()
    if not host:
        return False
    try:
        socket.getaddrinfo(host, None)
        return True
    except OSError as exc:
        logger.warning("EMAIL_HOST %r is not resolvable (%s); using console email backend", host, exc)
        return False


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
            # Never attempt SMTP against a bad/empty hostname (gaierror).
            if backend.endswith("smtp.EmailBackend") and not _smtp_host_usable():
                backend = "django.core.mail.backends.console.EmailBackend"
            self._fallback_connection = get_connection(backend=backend, fail_silently=self.fail_silently)
        return self._fallback_connection

    def send_messages(self, email_messages):
        config = MicrosoftGraphMailConfig.load()
        if not config.enabled:
            return self._safe_fallback_send(email_messages)

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
                logger.warning("Microsoft Graph mail failed (%s); trying fallback backend", exc)
                try:
                    return self._safe_fallback_send(email_messages)
                except Exception as fallback_exc:
                    if self.fail_silently:
                        logger.warning("Fallback mail send failed silently: %s", fallback_exc)
                        return 0
                    if isinstance(exc, MicrosoftGraphMailError):
                        raise
                    raise MicrosoftGraphMailError(str(exc)) from exc
        return sent

    def _safe_fallback_send(self, email_messages):
        try:
            return self._fallback().send_messages(email_messages)
        except OSError as exc:
            # socket.gaierror is an OSError subclass
            logger.error("Email send failed (network/DNS): %s — writing to console instead", exc)
            console = get_connection(
                backend="django.core.mail.backends.console.EmailBackend",
                fail_silently=self.fail_silently,
            )
            return console.send_messages(email_messages)

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
