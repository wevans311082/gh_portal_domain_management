"""Allauth adapter that records account mail and routes it to the right mailbox."""
import logging

from allauth.account import app_settings
from allauth.account.adapter import DefaultAccountAdapter
from allauth.core import context as allauth_context
from django.utils.encoding import force_str

from apps.accounts.mail_site import customer_domain_names, ensure_portal_site_record, portal_mail_site

logger = logging.getLogger(__name__)


class PortalAccountAdapter(DefaultAccountAdapter):
    """Send account mail through the portal backend and write an email log."""

    def format_email_subject(self, subject) -> str:
        subject = " ".join(force_str(subject).splitlines()).strip()
        prefix = app_settings.EMAIL_SUBJECT_PREFIX
        if prefix is None:
            prefix = f"[{portal_mail_site().name}] "
        if prefix and subject.lower().startswith(prefix.strip().lower()):
            return subject
        return f"{prefix}{subject}"

    def send_mail(self, template_prefix, email, context):
        ensure_portal_site_record()
        ctx = dict(context or {})
        site = portal_mail_site(ctx.get("request") or getattr(allauth_context, "request", None))
        domains = customer_domain_names(ctx.get("user"))
        ctx.update(
            {
                "email": email,
                "current_site": site,
                "portal_url": f"https://{site.domain}",
                "customer_domains": domains,
            }
        )
        msg = self.render_mail(template_prefix, email, ctx)
        extra = dict(getattr(msg, "extra_headers", None) or {})
        extra["X-CyberAsk-Mailbox-Purpose"] = "notifications"
        msg.extra_headers = extra
        try:
            msg.send()
        except Exception as exc:
            self._log_email(email, getattr(msg, "subject", ""), template_prefix, "failed", str(exc))
            logger.exception("Account email %s to %s failed", template_prefix, email)
            raise
        self._log_email(email, msg.subject, template_prefix, "sent", "")

    def _log_email(self, recipient, subject, template, status, error):
        try:
            from apps.audit.models import EmailLog

            EmailLog.objects.create(
                recipient=recipient or "",
                subject=(subject or "")[:255],
                template=(template or "")[:100],
                status=status,
                error=error or "",
            )
        except Exception:  # pragma: no cover - audit is best-effort
            logger.exception("Could not write email log for %s", recipient)
