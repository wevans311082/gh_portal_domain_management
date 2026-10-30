from unittest.mock import patch
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.domains.bulk_contacts import execute_bulk_contact, sync_bulk_contact
from apps.domains.bulk_reservations import release_item
from apps.domains.models import BulkDomainItem, DomainContactChange, DomainOperationReservation
from apps.domains.registrar_policy import ALL_ROLES
from tests.test_bulk_domain_admission import preview, confirm
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_contact_change_submission import api
from tests.test_contact_change_recovery import owner

pytestmark = pytest.mark.django_db(transaction=True)


def admitted_contact(current):
    domain, source = current
    batch = preview(current, action="contacts", target={"contacts": {role: source.registration_contact_id for role in ALL_ROLES}, "publication_choice": "redact"})
    confirm(batch)
    return batch.items.get()


def test_contact_item_admits_one_durable_child_and_reuses_on_replay(current):
    item = admitted_contact(current)
    with patch("apps.domains.contact_change_jobs.publish_contact_change", return_value=False) as publish:
        result = execute_bulk_contact(item.pk)
        replay = execute_bulk_contact(item.pk)
    assert result.status == replay.status == "awaiting"
    assert result.contact_change_id == replay.contact_change_id
    assert DomainContactChange.objects.count() == 1 and publish.call_count == 1
    child = result.contact_change
    assert child.status == "queued" and child.submitted_at is None
    assert result.submitted_at is None and DomainOperationReservation.objects.filter(item=item).exists()


@pytest.mark.parametrize("child_status", ["queued", "preparing", "awaiting", "unknown", "submitting"])
def test_pending_or_uncertain_child_never_becomes_parent_success(current, child_status):
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    DomainContactChange.objects.filter(pk=result.contact_change_id).update(status=child_status)
    result = sync_bulk_contact(item.pk)
    assert result.status == ("unknown" if child_status in ("unknown", "submitting") else "awaiting")
    assert DomainOperationReservation.objects.filter(item=item).exists()
    assert execute_bulk_contact(item.pk).contact_change_id == result.contact_change_id
    assert DomainContactChange.objects.count() == 1


def test_confirmed_child_projects_completion_without_repeating_provider_work(current):
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    finished = timezone.now()
    DomainContactChange.objects.filter(pk=result.contact_change_id).update(status="confirmed", confirmed_at=finished, submitted_at=finished, action_id="812")
    result = sync_bulk_contact(item.pk)
    assert result.status == "completed" and result.completed_at == finished and result.provider_action_id == "812"
    assert not DomainOperationReservation.objects.filter(item=item).exists()
    assert execute_bulk_contact(item.pk).status == "completed"
    assert DomainContactChange.objects.count() == 1


@pytest.mark.parametrize("submitted", [False, True])
def test_failed_child_releases_only_when_not_submitted(current, submitted):
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    DomainContactChange.objects.filter(pk=result.contact_change_id).update(status="failed", submitted_at=timezone.now() if submitted else None)
    result = sync_bulk_contact(item.pk)
    assert result.status == ("unknown" if submitted else "failed")
    assert DomainOperationReservation.objects.filter(item=item).exists() is submitted
    assert execute_bulk_contact(item.pk).contact_change_id == result.contact_change_id


def test_stale_proposed_contact_fails_before_child_admission(current):
    item = admitted_contact(current)
    contact = current[1].registration_contact
    contact.name = "Changed After Bulk Confirmation"
    contact.save(update_fields=["name"])
    result = execute_bulk_contact(item.pk)
    assert result.status == "failed" and not DomainContactChange.objects.exists()
    assert not DomainOperationReservation.objects.filter(item=item).exists()


def test_link_audit_failure_rolls_back_child_parent_and_broker_callback(current):
    item = admitted_contact(current)
    from apps.audit.models import AuditLog
    real_create = AuditLog.objects.create
    def audit(**kwargs):
        if kwargs["action"] == "domain.bulk.contact_linked":
            raise RuntimeError("audit unavailable")
        return real_create(**kwargs)
    with patch("apps.domains.contact_changes.AuditLog.objects.create", side_effect=audit), patch("apps.domains.contact_change_jobs.publish_contact_change") as publish:
        with pytest.raises(RuntimeError):
            execute_bulk_contact(item.pk)
    item.refresh_from_db()
    assert item.status == "queued" and item.contact_change_id is None
    assert not DomainContactChange.objects.exists() and not publish.called
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_forged_parent_completion_cannot_release_an_awaiting_child(current):
    item = admitted_contact(current)
    execute_bulk_contact(item.pk)
    BulkDomainItem.objects.filter(pk=item.pk).update(status="completed")
    with pytest.raises(ValidationError, match="linked contact change"):
        release_item(item)
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_changed_child_scope_cannot_be_projected_as_completion(current):
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    DomainContactChange.objects.filter(pk=result.contact_change_id).update(customer_id="different", status="confirmed", confirmed_at=timezone.now())
    with pytest.raises(ValidationError, match="association"):
        sync_bulk_contact(item.pk)
    item.refresh_from_db()
    assert item.status == "awaiting" and DomainOperationReservation.objects.filter(item=item).exists()


def test_child_worker_rechecks_parent_reservation_before_provider_work(current):
    from unittest.mock import MagicMock
    from apps.domains.contact_changes import submit_contact_change
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    DomainOperationReservation.objects.filter(item=item).delete()
    client = MagicMock()
    with pytest.raises(ValidationError, match="no longer admitted"):
        submit_contact_change(result.contact_change_id, client=client)
    assert not client.get_registration_reconciliation_details.called and not client.change_domain_contacts.called


def test_contact_progress_audit_failure_rolls_back_completion_and_slot_release(current):
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    now = timezone.now()
    DomainContactChange.objects.filter(pk=result.contact_change_id).update(status="confirmed", confirmed_at=now, submitted_at=now)
    with patch("apps.domains.bulk_contacts.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            sync_bulk_contact(item.pk)
    item.refresh_from_db()
    assert item.status == "awaiting" and DomainOperationReservation.objects.filter(item=item).exists()


def test_linked_pipeline_requires_real_contact_completion_and_irtp_before_releasing(current, api, owner):
    from datetime import timedelta
    from apps.domains.contact_changes import submit_contact_change
    from apps.domains.contact_change_confirmation import ContactChangeAction
    from apps.domains.contact_change_recovery import confirm_provider_completed
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    child_id = result.contact_change_id
    assert submit_contact_change(child_id, client=api) == "awaiting"
    assert sync_bulk_contact(item.pk).status == "awaiting"
    earlier = timezone.now() - timedelta(minutes=20)
    DomainContactChange.objects.filter(pk=child_id).update(submitted_at=earlier)
    api.get_contact_change_action.return_value = ContactChangeAction("501", "701", "ModifyContact", "ReviewedProviderCode", str(int((earlier + timedelta(minutes=1)).timestamp())))
    api.get_irtp_status.return_value = {"order-id": "501", "domain-name": item.name, "irtp-status": {
        "task-status": "PENDING", "losing-foa-status": "PENDING", "gaining-foa-status": "PENDING"}}
    for field in ("registrantcontactid", "admincontactid", "techcontactid", "billingcontactid"):
        api.get_registration_reconciliation_details.return_value[field] = "301"
    options = dict(owner_id=owner.pk, reference="CASE-BULK-701", reason="Reviewed exact successful contact action and registrant authorization.", provider_success_confirmed=True, client=api)
    with pytest.raises(ValidationError, match="authorization"):
        confirm_provider_completed(child_id, **options)
    assert sync_bulk_contact(item.pk).status == "awaiting" and DomainOperationReservation.objects.filter(item=item).exists()
    api.get_irtp_status.return_value["irtp-status"] = {"task-status": "SUCCESS", "losing-foa-status": "APPROVED", "gaining-foa-status": "APPROVED"}
    assert confirm_provider_completed(child_id, **options) == "confirmed"
    assert sync_bulk_contact(item.pk).status == "completed"
    assert not DomainOperationReservation.objects.filter(item=item).exists()
    assert api.change_domain_contacts.call_count == api.create_contact.call_count == 1
    assert execute_bulk_contact(item.pk).status == "completed"
