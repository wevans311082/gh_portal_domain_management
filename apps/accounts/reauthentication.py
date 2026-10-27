"""Verify both factors again before an authenticated user's sensitive action."""
from django.db import transaction
from django.utils import timezone

from .mfa import _consume_backup, _consume_totp, _reserve_attempt, credential_fingerprint
from .models import MFAAttemptWindow, User
from .verification import can_login


def confirm_identity(request, password, code):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if not can_login(user) or not user.mfa_enabled or request.session.get("impersonator_user_id"):
            return "unavailable"
        if not _reserve_attempt(user):
            return "limited"
        if not user.check_password(password):
            return "invalid"
        token = (code or "").strip()
        if not (_consume_totp(user, token) or _consume_backup(user, token)):
            return "invalid"
        MFAAttemptWindow.objects.filter(user=user).update(started_at=timezone.now(), attempts=0)
        request.session.cycle_key()
        request.session["_mfa_verified_at"] = timezone.now().timestamp()
        request.session["_mfa_verified_user_id"] = user.pk
        request.session["_mfa_verified_hash"] = credential_fingerprint(user)
        return ""
