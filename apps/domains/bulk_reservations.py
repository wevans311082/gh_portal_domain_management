"""Shared domain slots; lease expiry never proves that a mutation did not happen."""
from django.core.exceptions import ValidationError
from django.db import transaction
from apps.accounts.models import User
from .models import DomainOperationReservation, BulkDomainItem, Domain, DomainContactChange


class BulkOperationPaused(ValidationError):
    pass


def assert_no_bulk_operation(domain, *, item_id=None, dns_change_id=None):
    from .models import DomainDNSReservation
    dns_slots = DomainDNSReservation.objects.filter(domain_id=domain.pk)
    if dns_change_id is not None:
        dns_slots = dns_slots.exclude(change_id=dns_change_id)
    if dns_slots.exists():
        raise ValidationError("A DNS provider switch is reserved for this domain. Review its progress before editing or starting another operation.")
    reservations = DomainOperationReservation.objects.filter(domain_id=domain.pk)
    if item_id is not None:
        reservations = reservations.exclude(item_id=item_id)
    if reservations.exists():
        raise ValidationError("A bulk operation is already reserved for this domain. Review its progress before starting another operation.")


def assert_local_domain_editable(domain, *, dns_change_id=None):
    """Caller holds customer -> domain locks until its local edit commits."""
    from django.db.models import Q
    assert_no_bulk_operation(domain, dns_change_id=dns_change_id)
    changes = DomainContactChange.objects.filter(domain_id=domain.pk).filter(
        Q(status__in=["queued", "preparing", "submitting", "awaiting", "unknown"])
        | Q(submitted_at__isnull=False)
    ).exclude(status="confirmed", confirmed_at__isnull=False)
    if changes.exists():
        raise ValidationError("A contact operation is in progress or requires review before this local record can change.")


def reserve_item(item):
    """Caller holds the customer, domain and item locks inside its transaction."""
    if item.domain_id is None or item.status != "queued":
        raise ValidationError("Only a queued owned domain can be reserved.")
    reservation, _ = DomainOperationReservation.objects.get_or_create(domain_id=item.domain_id, defaults={"item_id": item.pk})
    if reservation.item_id != item.pk:
        raise ValidationError("Another operation already owns this domain slot.")
    return reservation


@transaction.atomic
def release_item(item):
    """Only known completion or proven pre-submission failure can free a slot."""
    identity = BulkDomainItem.objects.values("batch__user_id", "domain_id").get(pk=item.pk)
    User.objects.select_for_update().get(pk=identity["batch__user_id"])
    Domain.objects.select_for_update().get(pk=identity["domain_id"])
    item = BulkDomainItem.objects.select_for_update().get(pk=item.pk)
    if item.registrar_completed_at is not None and item.hosting_status not in ("not_required", "completed"):
        raise ValidationError("The hosting follow-up still retains this domain reservation.")
    if item.contact_change_id:
        child = DomainContactChange.objects.select_for_update().get(pk=item.contact_change_id)
        if not (item.status == "completed" and child.status == "confirmed" and child.confirmed_at is not None
                or item.status in ("failed", "cancelled") and child.status in ("failed", "cancelled") and child.submitted_at is None):
            raise ValidationError("The linked contact change still retains the domain reservation.")
    if item.status != "completed" and not (item.status in ("failed", "cancelled") and item.submitted_at is None):
        raise ValidationError("Unfinished or uncertain operations retain their domain reservation.")
    DomainOperationReservation.objects.filter(item_id=item.pk, domain_id=item.domain_id).delete()
