"""Email verification state and the messages that clear it."""
import logging

logger = logging.getLogger(__name__)


def email_is_verified(user) -> bool:
    """Superusers and legacy users without an EmailAddress row may sign in."""
    if getattr(user, "is_superuser", False):
        return True
    try:
        from allauth.account.models import EmailAddress
    except Exception:
        return True
    record = EmailAddress.objects.filter(user=user, email__iexact=user.email).first()
    if record is None:
        return True
    return bool(record.verified)


def can_login(user) -> bool:
    return bool(getattr(user, "is_active", False) and email_is_verified(user))


def mark_account_validated(user):
    """Record the account email as confirmed so the login gate allows it."""
    from allauth.account.models import EmailAddress

    EmailAddress.objects.filter(user=user).exclude(email__iexact=user.email).update(primary=False)
    record = EmailAddress.objects.filter(user=user, email__iexact=user.email).first()
    if record is None:
        return EmailAddress.objects.create(
            user=user,
            email=user.email,
            primary=True,
            verified=True,
        )
    record.email = user.email
    record.primary = True
    record.verified = True
    record.save(update_fields=["email", "primary", "verified"])
    return record


def allow_account_login(user):
    """Activate the account and mark its email confirmed."""
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])
    mark_account_validated(user)
    return user


def send_validation_email_if_needed(request, user) -> str:
    """Send the account confirmation email when the address is still unverified.

    Returns ``sent``, ``already_verified``, or ``not_sent``.
    """
    if email_is_verified(user):
        return "already_verified"
    from allauth.account.utils import send_email_confirmation

    sent = send_email_confirmation(request, user, signup=False, email=user.email)
    return "sent" if sent else "not_sent"


def validation_email_notice(request, user) -> str:
    """Send a validation email when required and return a short notice."""
    try:
        result = send_validation_email_if_needed(request, user)
    except Exception as exc:
        logger.exception("Validation email failed for %s", getattr(user, "email", ""))
        return f" The account is not validated, and the validation email failed: {exc}"
    if result == "sent":
        return " A validation email was sent because the account is not confirmed."
    if result == "not_sent":
        return " The account is not confirmed. A validation email is already waiting, or it could not be sent yet."
    return ""


def send_validation_after_password_reset(sender, request, user, **kwargs):
    """Allauth emits password_reset after the new password is saved."""
    notice = validation_email_notice(request, user)
    if not notice or request is None:
        return
    from django.contrib import messages

    messages.info(request, notice.strip())
