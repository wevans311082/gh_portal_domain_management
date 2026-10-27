import secrets
from datetime import timedelta
from typing import List

import pyotp
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.http import url_has_allowed_host_and_scheme

from apps.accounts.models import MFABackupCode, MFALoginChallenge, MFAAttemptWindow, User

CHALLENGE_SESSION_KEY = "_mfa_challenge_id"


def credential_fingerprint(user):
    """Invalidate pending authentication when a password or MFA secret changes."""
    return salted_hmac(
        "accounts.mfa.challenge",
        f"{user.pk}:{user.get_session_auth_hash()}:{user.mfa_secret}:{user.mfa_enabled}",
        algorithm="sha256",
    ).hexdigest()


def clear_challenge(request):
    challenge_id = request.session.pop(CHALLENGE_SESSION_KEY, None)
    request.session.pop("_mfa_pending_user_id", None)
    if challenge_id:
        try:
            MFALoginChallenge.objects.filter(pk=challenge_id, consumed_at__isnull=True).update(consumed_at=timezone.now())
        except (ValueError, ValidationError):
            pass


def start_challenge(request, user, return_to=""):
    """Call only after validating the primary authentication factor."""
    from .verification import can_login

    if not can_login(user) or not user.mfa_enabled or not user.mfa_secret:
        raise ValueError("The account cannot start an MFA challenge.")
    clear_challenge(request)
    if not url_has_allowed_host_and_scheme(
        return_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return_to = ""
    challenge = MFALoginChallenge.objects.create(
        user=user,
        expires_at=timezone.now() + timedelta(seconds=settings.MFA_CHALLENGE_LIFETIME_SECONDS),
        credential_hash=credential_fingerprint(user),
        return_to=return_to[:2048],
    )
    request.session.cycle_key()
    request.session[CHALLENGE_SESSION_KEY] = str(challenge.pk)
    request.session["_mfa_pending_user_id"] = user.pk
    return challenge


def pending_challenge(request):
    identifier = request.session.get(CHALLENGE_SESSION_KEY)
    if not identifier:
        return None
    try:
        challenge = MFALoginChallenge.objects.select_related("user").get(pk=identifier)
    except (MFALoginChallenge.DoesNotExist, ValueError, ValidationError):
        clear_challenge(request)
        return None
    from .verification import can_login

    if (
        challenge.consumed_at or challenge.expires_at <= timezone.now()
        or not can_login(challenge.user) or not challenge.user.mfa_enabled
        or not constant_time_compare(challenge.credential_hash, credential_fingerprint(challenge.user))
    ):
        clear_challenge(request)
        return None
    return challenge


def _reserve_attempt(user):
    """Caller holds the user row lock; invalid submissions count as attempts."""
    now = timezone.now()
    window, _ = MFAAttemptWindow.objects.get_or_create(user=user, defaults={"started_at": now})
    if now >= window.started_at + timedelta(seconds=settings.MFA_RATE_LIMIT_WINDOW_SECONDS):
        window.started_at, window.attempts = now, 0
    if window.attempts >= settings.MFA_MAX_ATTEMPTS:
        return False
    window.attempts += 1
    window.save(update_fields=["started_at", "attempts"])
    return True


def _consume_totp(user, token):
    if not user.mfa_secret or not token.isascii() or not token.isdigit() or len(token) != 6:
        return False
    try:
        totp = pyotp.TOTP(user.mfa_secret)
        step = int(timezone.now().timestamp()) // totp.interval
        for candidate in (step, step - 1, step + 1):
            if candidate <= user.mfa_last_verified_step:
                continue
            if constant_time_compare(totp.at(candidate * totp.interval), token):
                # CAS also prevents reuse on backends that do not implement row locks.
                updated = User.objects.filter(
                    pk=user.pk, mfa_last_verified_step__lt=candidate,
                    mfa_secret=user.mfa_secret, mfa_enabled=True, is_active=True,
                ).update(mfa_last_verified_step=candidate)
                if updated:
                    user.mfa_last_verified_step = candidate
                return bool(updated)
    except (ValueError, TypeError, OverflowError):
        return False
    return False


def _consume_backup(user, provided_code):
    normalized = (provided_code or "").strip().upper().replace(" ", "")
    if len(normalized) == 8 and "-" not in normalized:
        normalized = f"{normalized[:4]}-{normalized[4:]}"
    for backup in MFABackupCode.objects.filter(user=user, used_at__isnull=True).order_by("id"):
        if check_password(normalized, backup.code_hash):
            return bool(MFABackupCode.objects.filter(pk=backup.pk, used_at__isnull=True).update(
                used_at=timezone.now(), updated_at=timezone.now()
            ))
    return False


def verify_factor(user, token, *, allow_backup=True, allow_totp=True):
    """Consume one factor once, with a shared per-user attempt limit."""
    with transaction.atomic():
        current = User.objects.select_for_update().get(pk=user.pk)
        from .verification import can_login

        if not can_login(current) or not current.mfa_enabled or not _reserve_attempt(current):
            return False
        token = (token or "").strip()
        accepted = (allow_totp and _consume_totp(current, token)) or (allow_backup and _consume_backup(current, token))
        if accepted:
            MFAAttemptWindow.objects.filter(user=current).update(attempts=0, started_at=timezone.now())
        return bool(accepted)


def finish_challenge(request, token):
    """Return (user, destination, used_backup, error) after consuming a challenge."""
    challenge = pending_challenge(request)
    if not challenge:
        return None, "", False, "expired"
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=challenge.user_id)
        challenge = MFALoginChallenge.objects.select_for_update().get(pk=challenge.pk)
        from .verification import can_login

        if (challenge.consumed_at or challenge.expires_at <= timezone.now()
                or not can_login(user) or not user.mfa_enabled
                or not constant_time_compare(challenge.credential_hash, credential_fingerprint(user))):
            return None, "", False, "expired"
        if challenge.attempts >= settings.MFA_MAX_ATTEMPTS or not _reserve_attempt(user):
            return None, "", False, "limited"
        challenge.attempts += 1
        challenge.save(update_fields=["attempts"])
        token = (token or "").strip()
        totp_ok = _consume_totp(user, token)
        backup_ok = not totp_ok and _consume_backup(user, token)
        if not totp_ok and not backup_ok:
            return None, "", False, "invalid"
        claimed = MFALoginChallenge.objects.filter(pk=challenge.pk, consumed_at__isnull=True).update(consumed_at=timezone.now())
        if not claimed:
            return None, "", False, "expired"
        MFAAttemptWindow.objects.filter(user=user).update(attempts=0, started_at=timezone.now())
        request.session.pop(CHALLENGE_SESSION_KEY, None)
        request.session.pop("_mfa_pending_user_id", None)
        request.session["_mfa_verified_at"] = timezone.now().timestamp()
        request.session["_mfa_verified_user_id"] = user.pk
        request.session["_mfa_verified_hash"] = credential_fingerprint(user)
        return user, challenge.return_to, bool(backup_ok), ""


def generate_plain_backup_codes(count: int = 10) -> List[str]:
    codes: List[str] = []
    for _ in range(count):
        raw = secrets.token_hex(4).upper()
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def regenerate_backup_codes(user: User, count: int = 10) -> List[str]:
    plain_codes = generate_plain_backup_codes(count=count)
    with transaction.atomic():
        User.objects.select_for_update().get(pk=user.pk)
        MFABackupCode.objects.filter(user=user).delete()
        MFABackupCode.objects.bulk_create([
            MFABackupCode(user=user, code_hash=make_password(code)) for code in plain_codes
        ])
    return plain_codes


def consume_backup_code(user: User, provided_code: str) -> bool:
    return verify_factor(user, provided_code, allow_totp=False)


def active_backup_code_count(user: User) -> int:
    return MFABackupCode.objects.filter(user=user, used_at__isnull=True).count()
