import logging

from django.contrib.auth.backends import ModelBackend

from .models import User

logger = logging.getLogger(__name__)


class EmailBackend(ModelBackend):
    """
    Authenticate by e-mail address and password.

    If the account has MFA enabled, `authenticate()` returns `None` so that
    the standard login view rejects the request.  The caller must then
    separately validate the TOTP token via `verify_totp()` and call
    `django.contrib.auth.login()` directly once both factors are confirmed.

    This two-step approach keeps MFA enforcement inside the backend so it
    cannot be bypassed by other login paths (e.g. management commands,
    third-party packages) that call `authenticate()`.
    """

    def authenticate(self, request, email=None, password=None, totp_token=None, **kwargs):
        if not email or not password:
            return None

        try:
            user = User.objects.get(email__iexact=email.strip())
        except User.DoesNotExist:
            # Run the hasher anyway to prevent user-enumeration via timing
            User().set_password(password)
            return None

        if not user.check_password(password):
            return None

        if not self.user_can_authenticate(user):
            return None

        # --- MFA check ---
        if user.mfa_enabled:
            if not totp_token:
                logger.info(
                    "MFA required but no token supplied for user %s", user.pk
                )
                # Signal to the view that MFA is required without leaking
                # whether the password was correct
                return None
            if not self.verify_totp(user, totp_token):
                logger.warning(
                    "Invalid MFA token for user %s from IP %s",
                    user.pk,
                    request.META.get("REMOTE_ADDR", "unknown") if request else "unknown",
                )
                return None

        return user

    @staticmethod
    def verify_totp(user: User, token: str) -> bool:
        """Return True if *token* is a valid current TOTP code for *user*."""
        try:
            import pyotp  # soft dependency — only needed when MFA is in use
        except ImportError:
            logger.error(
                "pyotp is not installed but MFA is enabled for user %s. "
                "Install it: pip install pyotp",
                user.pk,
            )
            return False

        if not user.mfa_secret:
            return False

        totp = pyotp.TOTP(user.mfa_secret)
        # valid_window=1 accepts the previous/current/next 30-second window
        # to tolerate small clock skew between client and server
        return totp.verify(token, valid_window=1)
