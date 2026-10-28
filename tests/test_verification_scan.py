from datetime import timedelta
from unittest.mock import Mock, patch
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.models import Domain, DomainContact, DomainOrder, VerificationAction, VerificationQueueState
from apps.domains.verification import reconcile_registrant_verification
from apps.domains.verification_actions import (
    request_verification_action, schedule_verification_checks, run_verification_action,
)


@pytest.fixture
def owner(db, django_user_model):
    return django_user_model.objects.create_user(email="scan@example.com", password="testpass")


def make_order(owner, index=1, **changes):
    contact = DomainContact.objects.create(
        user=owner, label=f"Contact {index}", email=owner.email,
        registrant_type="individual", registrar_contact_id=str(1000 + index),
    )
    domain = Domain.objects.create(user=owner, name=f"scan-{index}.com", tld="com", status="active", registrar_id=str(2000 + index))
    values = dict(
        user=owner, domain=domain, domain_name=domain.name, tld="com", status="completed",
        registrar_order_id=str(2000 + index), registration_contact=contact,
        admin_contact=contact, tech_contact=contact, billing_contact=contact,
    )
    values.update(changes)
    return DomainOrder.objects.create(**values)


def client_for(order):
    return Mock(get_registrant_verification_details=Mock(return_value={
        "orderid": order.registrar_order_id, "domainname": order.domain_name, "customerid": "999",
        "registrantcontactid": order.registration_contact.registrar_contact_id,
        "registrantcontact": {"email": order.user.email}, "raaVerificationStatus": "Pending",
    }))


def test_scheduler_queues_readonly_work_as_system_without_provider_call(owner):
    order = make_order(owner)
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as client:
        assert schedule_verification_checks.run() == 1
        client.assert_not_called()
    job = VerificationAction.objects.get()
    assert job.is_automatic and job.actor_id is None and job.kind == "refresh"
    event = AuditLog.objects.get(action="domain.verification.requested")
    assert event.user_id is None and event.data["automatic"] is True
    assert VerificationQueueState.objects.get(pk=1).scan_after == order.pk


def test_system_worker_refreshes_without_sending_email(owner):
    order = make_order(owner)
    schedule_verification_checks.run()
    job = VerificationAction.objects.get()
    client = client_for(order)
    with patch("apps.domains.resellerclub_client.ResellerClubClient", return_value=client), patch(
        "apps.core.runtime_settings.get_runtime_setting", return_value="999"
    ):
        run_verification_action.run(job.pk)
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert order.verification_evidence.get().status == "pending"
    client.resend_registrant_verification.assert_not_called()


def test_automatic_path_cannot_request_email(owner):
    order = make_order(owner)
    with pytest.raises(ValidationError):
        request_verification_action(order.domain, None, "resend", uuid.uuid4(), automatic=True)
    assert not VerificationAction.objects.exists()


def test_fresh_provider_evidence_is_skipped(owner):
    order = make_order(owner)
    reconcile_registrant_verification(order.pk, "999", client=client_for(order))
    assert schedule_verification_checks.run() == 0
    assert not VerificationAction.objects.exists()


def test_stale_contact_evidence_is_rechecked(owner):
    order = make_order(owner)
    reconcile_registrant_verification(order.pk, "999", client=client_for(order))
    DomainContact.objects.filter(pk=order.registration_contact_id).update(email="new@example.com")
    assert schedule_verification_checks.run() == 1


@pytest.mark.parametrize("changes", [
    {"status": "cancelled"}, {"registrar_order_id": ""}, {"registrar_order_id": "invalid"},
    {"tld": "uk"},
])
def test_ineligible_orders_do_not_queue(owner, changes):
    make_order(owner, **changes)
    assert schedule_verification_checks.run() == 0
    assert not VerificationAction.objects.exists()


def test_inactive_owner_is_not_scanned(owner):
    make_order(owner)
    owner.is_active = False
    owner.save()
    assert schedule_verification_checks.run() == 0


def test_mismatched_stored_extension_is_skipped_before_admission(owner):
    make_order(owner, tld="net")  # Actual name ends in .com.
    assert schedule_verification_checks.run() == 0
    assert not VerificationAction.objects.exists()


def test_cursor_bounds_scan_and_reaches_orders_beyond_first_page(owner):
    orders = [make_order(owner, index) for index in range(101)]
    with patch("apps.domains.verification_actions.monotonic", return_value=0):
        assert schedule_verification_checks.run() == 100
    assert VerificationQueueState.objects.get(pk=1).scan_after == orders[99].pk
    assert schedule_verification_checks.run() == 1
    assert VerificationQueueState.objects.get(pk=1).scan_after == orders[100].pk
    assert schedule_verification_checks.run() == 0
    assert VerificationQueueState.objects.get(pk=1).scan_after == 0
    assert schedule_verification_checks.run() == 0  # Pending jobs deduplicate a new cycle.
    assert VerificationAction.objects.count() == 101


def test_time_budget_commits_partial_progress_and_next_scan_continues(owner):
    first = make_order(owner, 1)
    second = make_order(owner, 2)
    with patch("apps.domains.verification_actions.monotonic", side_effect=[0, 0, 9]):
        assert schedule_verification_checks.run() == 1
    assert VerificationQueueState.objects.get(pk=1).scan_after == first.pk
    assert schedule_verification_checks.run() == 1
    assert VerificationQueueState.objects.get(pk=1).scan_after == second.pk


def test_automatic_failure_has_fifteen_minute_backoff(owner):
    order = make_order(owner)
    schedule_verification_checks.run()
    job = VerificationAction.objects.get()
    VerificationAction.objects.filter(pk=job.pk).update(status="failed")
    VerificationQueueState.objects.filter(pk=1).update(scan_after=0)
    assert schedule_verification_checks.run() == 0
    VerificationAction.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(minutes=16))
    VerificationQueueState.objects.filter(pk=1).update(scan_after=0)
    assert schedule_verification_checks.run() == 1
    assert VerificationAction.objects.count() == 2


def test_deactivated_owner_cancels_system_job_before_provider_read(owner):
    make_order(owner)
    schedule_verification_checks.run()
    owner.is_active = False
    owner.save()
    job = VerificationAction.objects.get()
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as client:
        run_verification_action.run(job.pk)
        client.assert_not_called()
    job.refresh_from_db()
    assert job.status == "cancelled"


def test_automatic_scheduler_is_registered_and_scheduled():
    from cyberask_domains.celery import app
    from django.conf import settings

    assert "domains.schedule_verification_checks" in app.tasks
    assert settings.CELERY_BEAT_SCHEDULE["registrar-verification-scan"] == {
        "task": "domains.schedule_verification_checks", "schedule": 60.0,
    }


def test_full_queue_pauses_scan_without_losing_cursor(owner):
    first = make_order(owner, 1)
    make_order(owner, 2)
    request_verification_action(first.domain, owner, "refresh", uuid.uuid4())
    with patch("apps.domains.verification_actions.MAX_OPEN_ACTIONS", 1):
        assert schedule_verification_checks.run() == 0
    assert VerificationQueueState.objects.get(pk=1).scan_after == 0
    assert VerificationAction.objects.count() == 1


def test_worker_quota_defers_automatic_job_without_provider_work(owner):
    from apps.core.abuse import AbuseLimitExceeded

    make_order(owner)
    schedule_verification_checks.run()
    job = VerificationAction.objects.get()
    with patch("apps.core.abuse.consume_limit", side_effect=AbuseLimitExceeded(30)), patch(
        "apps.domains.resellerclub_client.ResellerClubClient"
    ) as client:
        run_verification_action.run(job.pk)
        client.assert_not_called()
    job.refresh_from_db()
    assert job.status == "queued"
    assert job.available_at > timezone.now()


def test_worker_cache_failure_defers_without_sending(owner):
    from apps.core.abuse import AbuseStoreUnavailable

    make_order(owner)
    schedule_verification_checks.run()
    job = VerificationAction.objects.get()
    with patch("apps.core.abuse.consume_limit", side_effect=AbuseStoreUnavailable), patch(
        "apps.domains.resellerclub_client.ResellerClubClient"
    ) as client:
        run_verification_action.run(job.pk)
        client.assert_not_called()
    job.refresh_from_db()
    assert job.status == "queued"
    assert job.available_at > timezone.now()


def test_customer_cannot_forge_automatic_identity(owner, client):
    from django.urls import reverse

    order = make_order(owner)
    client.force_login(owner)
    response = client.post(reverse("domains:verification_action", kwargs={"pk": order.domain_id}), {
        "kind": "refresh", "key": str(uuid.uuid4()), "automatic": "true",
    })
    assert response.status_code == 302
    job = VerificationAction.objects.get()
    assert not job.is_automatic
    assert job.actor_id == owner.pk


def test_dispatch_prioritizes_customer_requests_and_skips_deferred_work(owner):
    from apps.domains.verification_actions import dispatch_verification_actions

    first, second, third = [make_order(owner, index) for index in range(3)]
    automatic = request_verification_action(first.domain, None, "refresh", uuid.uuid4(), automatic=True)
    customer = request_verification_action(second.domain, owner, "refresh", uuid.uuid4())
    deferred = request_verification_action(third.domain, owner, "refresh", uuid.uuid4())
    VerificationAction.objects.filter(pk=deferred.pk).update(available_at=timezone.now() + timedelta(minutes=1))
    with patch("apps.domains.verification_actions.publish_verification_action") as publish:
        dispatch_verification_actions.run()
    assert [call.args[0] for call in publish.call_args_list] == [customer.pk, automatic.pk]
