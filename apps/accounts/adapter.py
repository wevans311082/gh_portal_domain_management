"""Allauth adapter that records account mail and routes it to the right mailbox."""
import logging

from allauth.account.adapter import DefaultAccountAdapter

logger = logging.getLogger(__name__)


class PortalAccountAdapter(DefaultAccountAdapter):
    """Send account mail through the portal backend and write an email log."""

    def send_mail(self, template_prefix, email, context):
        ctx = {
            "email": email,
            "current_site": self._current_site(context),
        }
        ctx.update(context)
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

    def _current_site(self, context):
        request = context.get("request") if isinstance(context, dict) else None
        if request is not None:
            from django.contrib.sites.shortcuts import get_current_site

            return get_current_site(request)
        from django.contrib.sites.models import Site

        return Site.objects.get_current()

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
