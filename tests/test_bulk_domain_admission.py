import uuid
from datetime import timedelta
from unittest.mock import patch
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.bulk_admission import create_bulk_preview, confirm_bulk
from apps.domains.models import BulkDomainBatch, BulkDomainItem
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db


def preview(current, **kwargs):
    domain, source = current
    values = {"customer": source.user, "key": uuid.uuid4(), "action": "lock", "domain_ids": [domain.pk], "target": {"enabled": False}}
    values.update(kwargs)
    return create_bulk_preview(**values)


def confirm(batch, **kwargs):
    values = {"customer": batch.user, "batch_id": batch.pk, "manifest_digest": batch.manifest_digest, "confirmed": True}
    values.update(kwargs)
    return confirm_bulk(**values)


def test_exact_preview_replay_and_confirmation_do_not_repeat_admission(current):
    key = uuid.uuid4()
    batch = preview(current, key=key)
    assert preview(current, key=key).pk == batch.pk
    assert BulkDomainBatch.objects.count() == BulkDomainItem.objects.count() == 1
    assert batch.items.get().status == "ready"
    confirm(batch)
    assert batch.items.get().status == "queued"
    # A late client retry must retain completed work even when its source changed.
    item = batch.items.get()
    item.status = "completed"
    item.save(update_fields=["status"])
    domain = current[0]
    domain.is_locked = False
    domain.save(update_fields=["is_locked"])
    confirm(batch)
    assert batch.items.get().status == "completed"
    assert AuditLog.objects.filter(action="domain.bulk.confirmed").count() == 1


def test_changed_key_identity_is_rejected(current):
    key = uuid.uuid4()
    preview(current, key=key)
    with pytest.raises(ValidationError, match="already been used"):
        preview(current, key=key, target={"enabled": True})
    assert BulkDomainBatch.objects.count() == 1


def test_foreign_confirmation_does_not_reveal_or_change_batch(current):
    batch = preview(current)
    foreign = User.objects.create_user(email="foreign-bulk-confirm@example.test", password="test")
    with pytest.raises(PermissionDenied):
        confirm(batch, customer=foreign)
    batch.refresh_from_db()
    assert batch.confirmed_at is None


@pytest.mark.parametrize("confirmed", [False, "true", 1, None])
def test_explicit_typed_confirmation_required(current, confirmed):
    batch = preview(current)
    with pytest.raises(ValidationError, match="Explicitly confirm"):
        confirm(batch, confirmed=confirmed)
    assert batch.items.get().status == "ready"


def test_expired_preview_requires_new_review(current):
    batch = preview(current)
    with patch("apps.domains.bulk_admission.timezone.now", return_value=batch.expires_at + timedelta(seconds=1)):
        with pytest.raises(ValidationError, match="expired"):
            confirm(batch)
    assert batch.items.get().status == "ready"


def test_stale_subject_prevents_confirmation(current):
    batch = preview(current)
    current[0].is_locked = False
    current[0].save(update_fields=["is_locked"])
    with pytest.raises(ValidationError, match="changed"):
        confirm(batch)
    batch.refresh_from_db()
    assert batch.confirmed_at is None and batch.items.get().status == "ready"


@pytest.mark.parametrize("tamper", ["batch", "item"])
def test_retained_manifest_detects_out_of_band_edits(current, tamper):
    batch = preview(current)
    if tamper == "batch":
        BulkDomainBatch.objects.filter(pk=batch.pk).update(target={"enabled": True})
    else:
        BulkDomainItem.objects.filter(batch=batch).update(snapshot={"forged": True})
    with pytest.raises(ValidationError, match="changed"):
        confirm(batch)
    assert batch.items.get().status == "ready"


def test_immutable_preview_fields_cannot_be_rewritten(current):
    batch = preview(current)
    batch.target = {"enabled": True}
    with pytest.raises(ValidationError, match="Retain"):
        batch.save()
    item = batch.items.get()
    item.name = "replacement.test"
    with pytest.raises(ValidationError, match="history"):
        item.save()


def test_preview_and_confirmation_audits_roll_back_with_records(current):
    with patch("apps.domains.bulk_admission.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            preview(current)
    assert not BulkDomainBatch.objects.exists() and not BulkDomainItem.objects.exists()
    batch = preview(current)
    with patch("apps.domains.bulk_admission.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            confirm(batch)
    batch.refresh_from_db()
    assert batch.confirmed_at is None and batch.items.get().status == "ready"


def test_mixed_selection_retains_held_items_and_queues_only_ready(current):
    batch = preview(current, domain_ids=[current[0].pk, 987654])
    confirm(batch)
    assert list(batch.items.values_list("status", flat=True)) == ["queued", "unavailable"]
    assert batch.items.get(selected_id=987654).domain_id is None


def test_all_held_preview_cannot_be_confirmed(current):
    batch = preview(current, domain_ids=[987654])
    with pytest.raises(ValidationError, match="No selected domains"):
        confirm(batch)


def test_contact_edit_after_preview_requires_new_confirmation(current):
    from apps.domains.registrar_policy import ALL_ROLES
    source = current[1]
    batch = preview(current, action="contacts", target={"contacts": {role: source.registration_contact_id for role in ALL_ROLES}, "publication_choice": "redact"})
    contact = source.registration_contact
    contact.name = "Changed Contact Name"
    contact.save(update_fields=["name"])
    with pytest.raises(ValidationError, match="changed"):
        confirm(batch)
    assert batch.items.get().status == "ready"


def test_local_unchanged_preference_finishes_without_inventing_provider_work(current):
    domain = current[0]
    domain.auto_renew = False
    domain.save(update_fields=["auto_renew"])
    batch = preview(current, action="auto_renew", target={"enabled": False})
    assert batch.items.get().preview_status == "unchanged"
    confirm(batch)
    item = batch.items.get()
    assert item.status == "completed" and item.completed_at is not None and item.submitted_at is None
    batch.refresh_from_db()
    batch.confirmed_at = None
    with pytest.raises(ValidationError, match="original bulk confirmation"):
        batch.save()
