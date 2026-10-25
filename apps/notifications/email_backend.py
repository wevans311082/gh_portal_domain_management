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

_UNCONFIGURED_MAIL = (
    "Outbound mail is not configured. Enable Microsoft 365 Graph with a mailbox, "
    "or set a reachable EMAIL_HOST. Console delivery is disabled."
)


def console_fallback_allowed() -> bool:
    return bool(getattr(settings, "EMAIL_ALLOW_CONSOLE_FALLBACK", False))


def outbound_mail_mode() -> str:
    """How customer and account mail will leave the process.

    ``graph`` sends through Microsoft Graph. ``smtp`` uses the fallback SMTP
    host. ``console`` only prints the message. ``unconfigured`` raises instead
    of pretending the message was sent.
    """
    config = MicrosoftGraphMailConfig.load()
    if config.enabled:
        try:
            config.validate()
            return "graph"
        except MicrosoftGraphMailError:
            if _smtp_host_usable():
                return "smtp"
            return "console" if console_fallback_allowed() else "unconfigured"
    if _smtp_host_usable():
        return "smtp"
    return "console" if console_fallback_allowed() else "unconfigured"


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

    def _fallback_backend_path(self) -> str:
        return getattr(
            settings,
            "M365_GRAPH_FALLBACK_EMAIL_BACKEND",
            "django.core.mail.backends.smtp.EmailBackend",
        )

    def _fallback(self):
        if self._fallback_connection is None:
            backend = self._fallback_backend_path()
            if backend.endswith("smtp.EmailBackend") and not _smtp_host_usable():
                if not console_fallback_allowed():
                    raise MicrosoftGraphMailError(_UNCONFIGURED_MAIL)
                backend = "django.core.mail.backends.console.EmailBackend"
            if backend.endswith("console.EmailBackend") and not console_fallback_allowed():
                raise MicrosoftGraphMailError(_UNCONFIGURED_MAIL)
            self._fallback_connection = get_connection(backend=backend, fail_silently=self.fail_silently)
        return self._fallback_connection

    def send_messages(self, email_messages):
        messages = list(email_messages or [])
        config = MicrosoftGraphMailConfig.load()
        if not config.enabled:
            return self._safe_fallback_send(messages)

        sent = 0
        client = MicrosoftGraphMailClient(config)
        for message in messages:
            try:
                purpose = self._purpose_for(message)
                mailbox = config.mailbox_for(purpose)
                config.validate(purpose=purpose)
                client.send_mail(mailbox=mailbox, message=self._graph_message(message))
                sent += 1
            except Exception as exc:
                logger.warning("Microsoft Graph mail failed (%s); trying fallback backend", exc)
                try:
                    sent += self._safe_fallback_send([message]) or 0
                except Exception as fallback_exc:
                    if self.fail_silently:
                        logger.warning("Fallback mail send failed silently: %s", fallback_exc)
                        continue
                    if isinstance(fallback_exc, MicrosoftGraphMailError):
                        raise
                    raise MicrosoftGraphMailError(str(fallback_exc)) from exc
        return sent

    def _safe_fallback_send(self, email_messages):
        try:
            return self._fallback().send_messages(email_messages)
        except MicrosoftGraphMailError:
            raise
        except OSError as exc:
            logger.error("Email send failed (network/DNS): %s", exc)
            if not console_fallback_allowed():
                if self.fail_silently:
                    return 0
                raise MicrosoftGraphMailError(_UNCONFIGURED_MAIL) from exc
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
