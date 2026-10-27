"""Time-bound, owner-authorized customer impersonation with mandatory audit."""
from django.conf import settings
from django.contrib.auth import login, logout
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.accounts.models import User
from apps.audit.models import AuditLog
from .access import is_owner, recent_mfa

CONTEXT_KEY = "owner_impersonation"


def start_impersonation(request, target, reason):
    actor = request.user
    if not is_owner(actor) or not recent_mfa(request) or request.session.get(CONTEXT_KEY):
        raise PermissionDenied("A recently verified platform owner is required.")
    reason = (reason or "").strip()
    if not reason or len(reason) > 500:
        raise ValidationError("Enter a support reason of no more than 500 characters.")
    if target.pk == actor.pk or target.is_staff or target.is_superuser or not target.is_active:
        raise PermissionDenied("Only an active customer account may be impersonated.")
    now = timezone.now().timestamp()
    context = {"owner_id": actor.pk, "owner_hash": actor.get_session_auth_hash(), "target_id": target.pk,
               "started_at": now, "reason": reason}
    with transaction.atomic():
        AuditLog.objects.create(
            user=actor, action="impersonation.started", model_name="accounts.User", object_id=str(target.pk),
            data={"target_user_id": target.pk, "reason": reason},
        )
        login(request, target, backend="apps.accounts.backends.EmailBackend")
        request.session[CONTEXT_KEY] = context
        request.session["impersonator_user_id"] = actor.pk
        request.session["impersonator_started_at"] = now


def validated_owner(request):
    context = request.session.get(CONTEXT_KEY)
    if not isinstance(context, dict):
        return None
    try:
        actor = User.objects.get(pk=context["owner_id"])
        age = timezone.now().timestamp() - float(context["started_at"])
        if (
            not is_owner(actor) or not actor.mfa_enabled
            or age < 0 or age >= settings.IMPERSONATION_LIFETIME_SECONDS
            or request.user.pk != context["target_id"]
            or not request.user.is_active or request.user.is_staff or request.user.is_superuser
            or not constant_time_compare(context["owner_hash"], actor.get_session_auth_hash())
        ):
            return None
    except (User.DoesNotExist, ValueError, TypeError, KeyError, ValidationError):
        return None
    return actor


def stop_impersonation(request):
    actor = validated_owner(request)
    if actor is None:
        logout(request)
        raise PermissionDenied("The owner session is no longer valid. Sign in again.")
    with transaction.atomic():
        AuditLog.objects.create(
            user=actor, action="impersonation.stopped", model_name="accounts.User", object_id=str(request.user.pk),
            data={"reason": request.session[CONTEXT_KEY]["reason"]},
        )
        login(request, actor, backend="apps.accounts.backends.EmailBackend")
        for name in (CONTEXT_KEY, "impersonator_user_id", "impersonator_started_at"):
            request.session.pop(name, None)
    # Do not restore a recent-MFA timestamp: another protected action must be
    # confirmed again after working inside a customer's session.

