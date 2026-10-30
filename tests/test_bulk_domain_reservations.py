import uuid
from unittest.mock import patch
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.domains.bulk_reservations import release_item, assert_no_bulk_operation
from apps.domains.models import DomainOperationReservation, BulkDomainItem, BulkDomainBatch
from apps.domains.contact_changes import request_contact_change
from apps.domains.registrar_policy import ALL_ROLES
from apps.domains.registrar_review import registrar_review_required
from tests.test_bulk_domain_admission import preview, confirm
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db


def test_confirmation_reserves_once_and_blocks_later_preview(current):
    batch = preview(current)
    confirm(batch)
    item = batch.items.get()
    assert DomainOperationReservation.objects.get(domain=current[0]).item_id == item.pk
    confirm(batch)
    assert DomainOperationReservation.objects.count() == 1
    later = preview(current)
    assert later.items.get().preview_status == "held"
    with pytest.raises(ValidationError, match="No selected domains"):
        confirm(later)


def test_confirmation_of_overlapping_old_preview_requires_new_review(current):
    first, second = preview(current), preview(current)
    confirm(first)
    with pytest.raises(ValidationError, match="changed"):
        confirm(second)
    second.refresh_from_db()
    assert second.confirmed_at is None and second.items.get().status == "ready"


@pytest.mark.parametrize("status,submitted", [("queued", False), ("preparing", False), ("submitting", True), ("unknown", True), ("awaiting", True), ("failed", True)])
def test_uncertain_or_unfinished_items_never_release_the_slot(current, status, submitted):
    batch = preview(current)
    confirm(batch)
    item = batch.items.get()
    BulkDomainItem.objects.filter(pk=item.pk).update(status=status, submitted_at=timezone.now() if submitted else None)
    # Even a forged/stale completed object is not sufficient to release a reservation.
    item.status = "completed"
    with pytest.raises(ValidationError, match="retain"):
        release_item(item)
    assert DomainOperationReservation.objects.filter(item=item).exists()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_known_terminal_work_can_release_and_new_preview_can_proceed(current, status):
    batch = preview(current)
    confirm(batch)
    item = batch.items.get()
    item.status = status
    item.save(update_fields=["status"])
    release_item(item)
    release_item(item)  # Exact cleanup retry is harmless.
    assert not DomainOperationReservation.objects.exists()
    assert preview(current).items.get().preview_status == "ready"


def test_standalone_contact_proposal_and_provider_review_see_the_reservation(current):
    batch = preview(current)
    confirm(batch)
    domain, source = current
    assert registrar_review_required(domain)
    assert not registrar_review_required(domain, bulk_item_id=batch.items.get().pk)
    with pytest.raises(ValidationError, match="reserved"):
        request_contact_change(customer=source.user, domain_id=domain.pk, key=uuid.uuid4(), contacts={role: source.registration_contact_id for role in ALL_ROLES})


def test_confirmation_failure_rolls_back_reservation_and_status(current):
    batch = preview(current)
    with patch("apps.domains.bulk_admission.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            confirm(batch)
    assert not DomainOperationReservation.objects.exists()
    assert batch.items.get().status == "ready"
