"""Prevent accidentally restoring the password-only authentication bypass."""
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security)
def check_authentication_policy(app_configs, **kwargs):
    if settings.AUTHENTICATION_BACKENDS != ["apps.accounts.backends.EmailBackend"]:
        return [Error(
            "Authentication backends must enforce the shared portal MFA policy.",
            hint="Use apps.accounts.backends.EmailBackend; do not add a password-only fallback backend.",
            id="accounts.E001",
        )]
    return []
