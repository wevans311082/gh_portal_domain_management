"""Explicit pre-mutation cancellation. Never releases uncertain provider work."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.audit.models import AuditLog
from .contact_bindings import _fingerprint
from .dns_changes import _context, _manifest
from .models import DomainDNSChange, DomainDNSReservation


CANCELLATION_REASONS = frozenset({"user_cancelled", "replace_expired_plan", "source_changed"})
PRE_MUTATION_STATES = frozenset({"draft", "inventory", "plan_review", "plan_approved", "review"})


@transaction.atomic
def cancel_dns_change(*, actor, change_id, manifest_digest, confirmed, reason):
    """Release only local, positively pre-mutation intent; retain all evidence.

    An inventory read may be in flight. Clearing its token and changing status
    fences its final commit, even if that external read later succeeds. This
    exception is safe only for the read-only inventory stage. Mutation stages
    must never be added to PRE_MUTATION_STATES to bypass reconciliation.
    """
    if confirmed is not True or type(reason) is not str or reason not in CANCELLATION_REASONS:
        raise ValidationError("Explicitly confirm cancellation and choose a valid reason.")
    identity = DomainDNSChange.objects.filter(pk=change_id).values("domain_id").first()
    if identity is None:
        raise PermissionDenied
    actor, domain = _context(actor, identity["domain_id"])
    change = DomainDNSChange.objects.select_for_update().get(pk=change_id)
    if (type(manifest_digest) is not str or not constant_time_compare(manifest_digest, change.manifest_digest)
            or change.manifest_digest != _manifest(change)):
        raise ValidationError("The retained DNS request requires review before cancellation.")
    if change.inventory_digest and change.inventory_digest != _fingerprint(change.inventory_receipt):
        raise ValidationError("The retained DNS inventory requires review before cancellation.")
    if change.status == "cancelled" and change.cancelled_at is not None and change.cancelled_by_id is not None:
        if reason != change.cancellation_reason or DomainDNSReservation.objects.filter(change=change).exists():
            raise ValidationError("The retained DNS cancellation requires review.")
        return change
    if (change.status not in PRE_MUTATION_STATES or change.cancelled_at is not None
            or change.zone_submitted_at is not None or change.zone_id or change.zone_observation
            or change.completed_at is not None):
        raise ValidationError("Provider work may have started. Reconcile this DNS request before releasing it.")
    reservation = DomainDNSReservation.objects.select_for_update().filter(domain=domain).first()
    if change.confirmed_at is not None:
        if reservation is None or reservation.change_id != change.pk:
            raise ValidationError("This DNS request's reservation requires review.")
    elif change.status != "draft" or (reservation is not None and reservation.change_id == change.pk):
        raise ValidationError("This DNS request's confirmation requires review.")
    change.status, change.cancelled_at, change.cancelled_by = "cancelled", timezone.now(), actor
    change.cancellation_reason = reason
    change.inventory_token = change.inventory_lease_until = None
    change.inventory_message = "Cancelled before provider changes. Review a new request to choose DNS settings."
    change.save(update_fields=["status", "cancelled_at", "cancelled_by", "cancellation_reason",
        "inventory_token", "inventory_lease_until", "inventory_message", "updated_at"])
    if reservation is not None and reservation.change_id == change.pk:
        reservation.delete()
    AuditLog.objects.create(user=actor, action="domain.dns.cancelled", model_name="DomainDNSChange",
        object_id=str(change.pk), data={"domain_id": domain.pk, "reason": reason})
    return change
