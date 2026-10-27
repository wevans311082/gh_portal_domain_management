from django.contrib.auth.backends import ModelBackend

from .models import User

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
        email = email or kwargs.get("username")
        user = self.authenticate_password(email, password)
        if user is None:
            return None

        # A primary factor is never sufficient for an MFA-enabled account.
        if user.mfa_enabled and (not totp_token or not self.verify_totp(user, totp_token)):
            return None
        return user

    def authenticate_password(self, email, password):
        """Verify only the primary factor; callers must not treat this as a login."""
        if not email or not password:
            return None

        try:
            user = User.objects.get(email__iexact=email.strip())
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            # Run the hasher anyway to prevent user-enumeration via timing
            User().set_password(password)
            return None

        if not user.check_password(password):
            return None

        if not self.user_can_authenticate(user):
            return None

        return user

    @staticmethod
    def verify_totp(user: User, token: str) -> bool:
        """Consume a valid, unused TOTP code within the account attempt limit."""
        from .mfa import verify_factor

        return verify_factor(user, token, allow_backup=False)
