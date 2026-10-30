import uuid
from unittest.mock import patch
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.bulk_admission import create_bulk_preview, confirm_bulk
from apps.domains.bulk_execution import execute_local_preference
from apps.domains.bulk_retry import create_retry_preview
from apps.domains.models import Domain, BulkDomainBatch, BulkDomainItem
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_contact_change_submission import api

pytestmark = pytest.mark.django_db


@pytest.fixture
def failed_batch():
    customer = User.objects.create_user(email="selective-retry@example.test", password="test")
    first = Domain.objects.create(user=customer, name="retry-first.test", tld="test", auto_renew=True)
    second = Domain.objects.create(user=customer, name="retry-second.test", tld="test", auto_renew=False)
    batch = create_bulk_preview(customer=customer, key=uuid.uuid4(), action="auto_renew", domain_ids=[first.pk, second.pk], target={"enabled": False})
    confirm_bulk(customer=customer, batch_id=batch.pk, manifest_digest=batch.manifest_digest, confirmed=True)
    failed, completed = list(batch.items.all())
    User.objects.filter(pk=customer.pk).update(is_active=False)
    assert execute_local_preference(failed.pk).status == "failed"
    User.objects.filter(pk=customer.pk).update(is_active=True)
    batch.refresh_from_db()
    failed.refresh_from_db()
    completed.refresh_from_db()
    return customer, batch, failed, completed


def retry(failed_batch, **kwargs):
    customer, batch, failed, completed = failed_batch
    values = dict(customer=customer, batch_id=batch.pk, item_ids=[failed.pk], key=uuid.uuid4())
    values.update(kwargs)
    return create_retry_preview(**values)


def test_retry_creates_fresh_selected_preview_and_preserves_original_outcomes(failed_batch):
    customer, original, failed, completed = failed_batch
    batch = retry(failed_batch)
    assert batch.retry_of_id == original.pk and batch.retry_item_ids == [failed.pk]
    assert batch.retry_source_digest == original.manifest_digest
    assert batch.confirmed_at is None and batch.items.count() == 1
    assert batch.items.get().selected_id == failed.selected_id
    assert batch.items.get().status == "ready"
    confirm_bulk(customer=customer, batch_id=batch.pk, manifest_digest=batch.manifest_digest, confirmed=True)
    assert execute_local_preference(batch.items.get().pk).status == "completed"
    failed.refresh_from_db()
    completed.refresh_from_db()
    assert failed.status == "failed" and completed.status == "completed"
    assert original.items.count() == 2


def test_exact_retry_key_replay_retains_its_history_without_repeating_admission(failed_batch):
    customer, original, failed, completed = failed_batch
    key = uuid.uuid4()
    batch = retry(failed_batch, key=key)
    confirm_bulk(customer=customer, batch_id=batch.pk, manifest_digest=batch.manifest_digest, confirmed=True)
    assert retry(failed_batch, key=key).pk == batch.pk
    execute_local_preference(batch.items.get().pk)
    assert retry(failed_batch, key=key).pk == batch.pk
    assert batch.items.get().status == "completed"
    assert BulkDomainBatch.objects.count() == 2


@pytest.mark.parametrize("status", ["completed", "queued", "preparing", "submitting", "awaiting", "unknown"])
def test_completed_active_and_uncertain_source_items_cannot_be_retried(failed_batch, status):
    failed = failed_batch[2]
    BulkDomainItem.objects.filter(pk=failed.pk).update(status=status)
    with pytest.raises(ValidationError, match="Retry only"):
        retry(failed_batch)
    assert BulkDomainBatch.objects.count() == 1


def test_submission_marker_prevents_retry_even_if_label_says_failed(failed_batch):
    BulkDomainItem.objects.filter(pk=failed_batch[2].pk).update(submitted_at=timezone.now())
    with pytest.raises(ValidationError, match="proven not"):
        retry(failed_batch)


def test_selection_must_be_explicit_distinct_and_from_the_same_owned_batch(failed_batch):
    completed = failed_batch[3]
    for selection in ([], [True], [failed_batch[2].pk] * 2, [987654], [completed.pk]):
        with pytest.raises(ValidationError):
            retry(failed_batch, item_ids=selection)
    foreign = User.objects.create_user(email="foreign-selective-retry@example.test", password="test")
    with pytest.raises(PermissionDenied):
        retry(failed_batch, customer=foreign)


def test_source_eligibility_is_rechecked_before_fresh_confirmation(failed_batch):
    customer, original, failed, completed = failed_batch
    batch = retry(failed_batch)
    BulkDomainItem.objects.filter(pk=failed.pk).update(status="unknown", submitted_at=timezone.now())
    with pytest.raises(ValidationError, match="Retry only"):
        confirm_bulk(customer=customer, batch_id=batch.pk, manifest_digest=batch.manifest_digest, confirmed=True)
    batch.refresh_from_db()
    assert batch.confirmed_at is None and batch.items.get().status == "ready"


def test_retry_lineage_cannot_be_rewritten_or_confused_with_a_normal_request_key(failed_batch):
    customer, original, failed, completed = failed_batch
    batch = retry(failed_batch)
    batch.retry_item_ids = [completed.pk]
    with pytest.raises(ValidationError, match="Retain"):
        batch.save()
    with pytest.raises(ValidationError, match="already been used"):
        create_bulk_preview(customer=customer, key=batch.key, action="auto_renew", domain_ids=[failed.selected_id], target={"enabled": False})


def test_retry_audit_failure_rolls_back_fresh_batch_and_preserves_source(failed_batch):
    real_create = AuditLog.objects.create
    def audit(**kwargs):
        if kwargs["action"] == "domain.bulk.retry_previewed":
            raise RuntimeError("audit unavailable")
        return real_create(**kwargs)
    with patch("apps.domains.bulk_admission.AuditLog.objects.create", side_effect=audit):
        with pytest.raises(RuntimeError):
            retry(failed_batch)
    assert BulkDomainBatch.objects.count() == 1
    failed_batch[2].refresh_from_db()
    assert failed_batch[2].status == "failed"


def test_two_retry_previews_cannot_both_reserve_the_same_failed_domain(failed_batch):
    customer = failed_batch[0]
    first, second = retry(failed_batch), retry(failed_batch)
    confirm_bulk(customer=customer, batch_id=first.pk, manifest_digest=first.manifest_digest, confirmed=True)
    with pytest.raises(ValidationError, match="Retry only"):
        confirm_bulk(customer=customer, batch_id=second.pk, manifest_digest=second.manifest_digest, confirmed=True)
    second.refresh_from_db()
    assert second.confirmed_at is None


def test_corrected_target_requires_its_own_fresh_policy_review_and_confirmation(failed_batch):
    batch = retry(failed_batch, target={"enabled": True})
    assert batch.target == {"enabled": True} and batch.confirmed_at is None
    assert failed_batch[1].target == {"enabled": False}
    # Enabling is rechecked and held for these unreviewed fixture domains.
    assert batch.items.get().status == "held"
    assert BulkDomainItem.objects.get(pk=failed_batch[2].pk).status == "failed"


@pytest.mark.django_db(transaction=True)
def test_contact_retry_uses_a_new_child_only_after_known_pre_submission_failure(current, api):
    from tests.test_bulk_domain_contacts import admitted_contact
    from apps.domains.bulk_contacts import execute_bulk_contact, sync_bulk_contact
    from apps.domains.contact_changes import submit_contact_change
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    original_child_id = result.contact_change_id
    api.get_registration_reconciliation_details.return_value["customerid"] = "888"
    with pytest.raises(ValidationError):
        submit_contact_change(original_child_id, client=api)
    assert sync_bulk_contact(item.pk).status == "failed"
    batch = create_retry_preview(customer=current[1].user, batch_id=item.batch_id, item_ids=[item.pk], key=uuid.uuid4())
    confirm_bulk(customer=current[1].user, batch_id=batch.pk, manifest_digest=batch.manifest_digest, confirmed=True)
    retried = execute_bulk_contact(batch.items.get().pk)
    assert retried.contact_change_id != original_child_id
    item.refresh_from_db()
    assert item.status == "failed" and item.contact_change_id == original_child_id
    assert not api.create_contact.called and not api.change_domain_contacts.called


@pytest.mark.django_db(transaction=True)
def test_child_submission_marker_cannot_be_hidden_by_stale_parent_failure(current):
    from tests.test_bulk_domain_contacts import admitted_contact
    from apps.domains.bulk_contacts import execute_bulk_contact
    from apps.domains.models import DomainContactChange, DomainOperationReservation
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    DomainContactChange.objects.filter(pk=result.contact_change_id).update(status="failed", submitted_at=timezone.now())
    BulkDomainItem.objects.filter(pk=item.pk).update(status="failed", submitted_at=None)
    DomainOperationReservation.objects.filter(item=item).delete()
    with pytest.raises(ValidationError, match="linked contact request"):
        create_retry_preview(customer=current[1].user, batch_id=item.batch_id, item_ids=[item.pk], key=uuid.uuid4())
