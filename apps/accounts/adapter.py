"""Allauth adapter that records account mail and routes it to the right mailbox."""
from allauth.account import app_settings
from allauth.account.adapter import DefaultAccountAdapter
from allauth.core import context as allauth_context
from django.utils.encoding import force_str
from django.shortcuts import redirect
from allauth.core.exceptions import ImmediateHttpResponse

from apps.accounts.mail_site import customer_domain_names, ensure_portal_site_record, portal_mail_site

class PortalAccountAdapter(DefaultAccountAdapter):
    """Queue encrypted account mail for the bounded delivery worker."""

    def pre_login(self, request, user, **kwargs):
        response = super().pre_login(request, user, **kwargs)
        if response:
            return response
        from .verification import can_login
        from .mfa import start_challenge

        if user.mfa_enabled and can_login(user):
            if not user.mfa_secret:
                return redirect("account_login")
            start_challenge(request, user, kwargs.get("redirect_url") or "")
            return redirect("accounts_custom:mfa_verify")

    def login(self, request, user):
        # Defence in depth: allauth's resume flows can reach login without pre_login.
        from .mfa import start_challenge
        from .verification import can_login

        # An email-verification stage may have been pending while account state changed.
        user.refresh_from_db()
        if not can_login(user):
            raise ImmediateHttpResponse(redirect("account_login"))
        if user.mfa_enabled:
            if not user.mfa_secret:
                raise ImmediateHttpResponse(redirect("account_login"))
            start_challenge(request, user)
            raise ImmediateHttpResponse(redirect("accounts_custom:mfa_verify"))
        return super().login(request, user)

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
        from .mail_delivery import queue_account_mail
        from apps.core.public_submissions import SubmissionUnavailable
        try:
            queue_account_mail(msg, template_prefix, ctx.get("user"))
        except SubmissionUnavailable:
            # Token-bearing mail must never fall back to synchronous sending.
            request = ctx.get("request") or getattr(allauth_context, "request", None)
            if request is not None and getattr(request, "resolver_match", None):
                from apps.core.abuse import PublicEndpointBoundaryMiddleware
                response = PublicEndpointBoundaryMiddleware.error(
                    request, "Account mail is temporarily unavailable. Please try again later.", 503)
                response["Retry-After"] = "60"
                raise ImmediateHttpResponse(response) from None
            raise
