from unittest.mock import patch
import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.bulk_controls import set_bulk_paused
from apps.domains.bulk_jobs import run_bulk_item, dispatch_bulk_items
from apps.domains.bulk_remote import execute_remote_lock
from apps.domains.bulk_reservations import BulkOperationPaused
from apps.domains.models import DomainOperationReservation
from tests.test_bulk_domain_remote import admitted, Provider
from tests.test_bulk_domain_contacts import admitted_contact
from tests.test_bulk_domain_completion import review_setup, review
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_contact_change_submission import api

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def operator():
    return User.objects.create_user(email="bulk-pause-owner@example.test", password="test", is_staff=True, is_superuser=True)


def control(item, operator, paused):
    return set_bulk_paused(item.pk, owner_id=operator.pk, paused=paused, reason="Review scheduling for this exact retained bulk item.")


def test_pause_is_idempotent_blocks_new_work_and_resume_keeps_original_target(current, operator):
    item = admitted(current)
    first = control(item, operator, True)
    token = first.pause_token
    assert first.paused_by_id == operator.pk
    assert control(item, operator, True).pause_token == token
    with override_settings(DOMAIN_BULK_EXECUTION_ENABLED=True), patch("apps.domains.bulk_remote.execute_remote_lock", side_effect=AssertionError("No paused worker")):
        run_bulk_item(item.pk)
    with override_settings(DOMAIN_BULK_EXECUTION_ENABLED=True, CELERY_TASK_ALWAYS_EAGER=False), patch("apps.domains.bulk_jobs.run_bulk_item.delay") as broker:
        from apps.domains.bulk_jobs import publish_bulk_item
        assert publish_bulk_item(item.pk) is False and not broker.called
    provider = Provider(item)
    assert execute_remote_lock(item.pk, client=provider).status == "queued" and not provider.calls
    assert DomainOperationReservation.objects.filter(item=item).exists()
    resumed = control(item, operator, False)
    assert resumed.paused_at is None and resumed.pause_token != token and resumed.status == "queued"
    assert execute_remote_lock(item.pk, client=provider).status == "awaiting"
    assert provider.calls == [("unlock", "501")]
    assert AuditLog.objects.filter(action="domain.bulk.paused").count() == 1


def test_pause_during_admitted_http_retains_its_acknowledgement(current, operator):
    item = admitted(current)
    provider = Provider(item, hook=lambda: control(item, operator, True))
    result = execute_remote_lock(item.pk, client=provider)
    assert result.status == "awaiting" and result.paused_at is not None and result.provider_action_id == "812"
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_paused_contact_child_is_not_dispatched_or_failed(current, operator):
    from apps.domains.bulk_contacts import execute_bulk_contact
    from apps.domains.contact_change_jobs import run_contact_change, dispatch_contact_changes
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    child_id = result.contact_change_id
    control(item, operator, True)
    with patch("apps.domains.resellerclub_client.ResellerClubClient", side_effect=AssertionError("No paused child client")):
        run_contact_change(child_id)
    result.contact_change.refresh_from_db()
    assert result.contact_change.status == "queued"
    with override_settings(DOMAIN_BULK_EXECUTION_ENABLED=True, CELERY_TASK_ALWAYS_EAGER=False), patch("apps.domains.bulk_jobs.publish_bulk_item") as publish, patch("apps.domains.contact_change_jobs.publish_contact_change") as child_publish:
        assert dispatch_bulk_items() == 0
        dispatch_contact_changes()
    assert not publish.called and not child_publish.called


def test_contact_pause_during_preparation_can_resume_without_duplicate_contact_creation(current, operator, api):
    from apps.domains.bulk_contacts import execute_bulk_contact
    from apps.domains.contact_changes import submit_contact_change
    item = admitted_contact(current)
    result = execute_bulk_contact(item.pk)
    def created(payload):
        control(item, operator, True)
        return "301"
    api.create_contact.side_effect = created
    with pytest.raises(BulkOperationPaused):
        submit_contact_change(result.contact_change_id, client=api)
    result.contact_change.refresh_from_db()
    assert result.contact_change.status == "queued" and result.contact_change.submitted_at is None
    assert not api.change_domain_contacts.called
    control(item, operator, False)
    assert submit_contact_change(result.contact_change_id, client=api) == "awaiting"
    assert api.create_contact.call_count == api.change_domain_contacts.call_count == 1


@pytest.mark.parametrize("value", ["true", 1, None])
def test_typed_control_and_active_owner_are_required(current, operator, value):
    item = admitted(current)
    with pytest.raises(ValidationError):
        set_bulk_paused(item.pk, owner_id=operator.pk, paused=value, reason="A bounded control review reason.")
    with pytest.raises(ValidationError, match="active owner"):
        control(item, current[1].user, True)


def test_pause_audit_failure_rolls_back_controls_without_freeing_slot(current, operator):
    item = admitted(current)
    with patch("apps.domains.bulk_controls.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            control(item, operator, True)
    item.refresh_from_db()
    assert item.paused_at is None and item.pause_token is None
    assert DomainOperationReservation.objects.filter(item=item).exists()


@pytest.mark.parametrize("repause", [False, True])
def test_resume_or_resume_pause_invalidates_inflight_owner_completion_review(current, repause):
    item, operator, client = review_setup(current)
    def changed():
        control(item, operator, False)
        if repause:
            control(item, operator, True)
    client.hook = changed
    with pytest.raises(ValidationError, match="retained review changed"):
        review(item, operator, client)
    item.refresh_from_db()
    assert item.status == "awaiting" and DomainOperationReservation.objects.filter(item=item).exists()


def test_success_attestation_cannot_replace_a_recorded_pause(current):
    from datetime import timedelta
    from django.utils import timezone
    from apps.domains.models import BulkDomainItem
    item, operator, client = review_setup(current)
    control(item, operator, False)
    BulkDomainItem.objects.filter(pk=item.pk).update(updated_at=timezone.now() - timedelta(minutes=20))
    with pytest.raises(ValidationError, match="Stop/review workers"):
        review(item, operator, client)
    assert not client.reads


def test_resume_audit_failure_keeps_pause_and_does_not_publish(current, operator):
    item = admitted(current)
    paused = control(item, operator, True)
    token = paused.pause_token
    with patch("apps.domains.bulk_controls.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")), patch("apps.domains.bulk_jobs.publish_bulk_item") as publish:
        with pytest.raises(RuntimeError):
            control(item, operator, False)
    item.refresh_from_db()
    assert item.paused_at is not None and item.pause_token == token and not publish.called
