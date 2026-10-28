from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.abuse import AbuseStoreUnavailable
from apps.domains.contact_change_jobs import dispatch_contact_changes, publish_contact_change, run_contact_change
from apps.domains.models import DomainContactChange
from tests.test_contact_change_admission import current, request
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


def test_eager_publishing_does_not_run_provider_work(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    with patch("apps.domains.contact_change_jobs.run_contact_change.delay") as send:
        assert publish_contact_change(123) is False
    send.assert_not_called()


def test_broker_failure_preserves_durable_request(current, settings):
    job = request(current)
    settings.CELERY_TASK_ALWAYS_EAGER = False
    with patch("apps.domains.contact_change_jobs.run_contact_change.delay", side_effect=ConnectionError):
        assert publish_contact_change(job.pk) is False
    assert DomainContactChange.objects.get(pk=job.pk).status == "queued"


def test_worker_claims_lease_before_constructing_client_and_submitting(current):
    job = request(current)
    def submit(identifier, *, client):
        claimed = DomainContactChange.objects.get(pk=identifier)
        assert claimed.lease_until > timezone.now()
        run_contact_change.run(identifier)  # overlapping delivery must do nothing
        DomainContactChange.objects.filter(pk=identifier).update(status="awaiting", action_id="701")
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as factory, patch(
        "apps.domains.contact_changes.submit_contact_change", side_effect=submit) as submitter:
        run_contact_change.run(job.pk)
    assert factory.call_count == submitter.call_count == 1
    job.refresh_from_db()
    assert job.status == "awaiting" and job.lease_until is None


@pytest.mark.parametrize("state,action_id,expected", [("unknown", "", False), ("unknown", "701", True),
    ("awaiting", "701", True), ("preparing", "", False), ("submitting", "", False), ("confirmed", "701", False)])
def test_worker_only_observes_known_actions_and_never_resubmits_uncertain_work(current, state, action_id, expected):
    job = request(current)
    DomainContactChange.objects.filter(pk=job.pk).update(status=state, action_id=action_id)
    with patch("apps.domains.resellerclub_client.ResellerClubClient"), patch(
        "apps.domains.contact_changes.submit_contact_change") as submitter, patch(
        "apps.domains.contact_changes.observe_contact_change") as observer:
        run_contact_change.run(job.pk)
    submitter.assert_not_called()
    assert observer.called is expected


def test_shared_limiter_failure_defers_before_client_creation(current):
    job = request(current)
    with patch("apps.core.abuse.consume_limit", side_effect=AbuseStoreUnavailable), patch(
        "apps.domains.resellerclub_client.ResellerClubClient") as factory:
        run_contact_change.run(job.pk)
    factory.assert_not_called()
    job.refresh_from_db()
    assert job.status == "queued" and job.available_at > timezone.now()


@pytest.mark.parametrize("state", ["preparing", "submitting"])
def test_dispatcher_quarantines_interrupted_mutation_without_republishing(current, state):
    job = request(current)
    DomainContactChange.objects.filter(pk=job.pk).update(status=state, lease_until=timezone.now() - timedelta(seconds=1))
    with patch("apps.domains.contact_change_jobs.publish_contact_change") as publisher:
        dispatch_contact_changes.run()
    publisher.assert_not_called()
    assert DomainContactChange.objects.get(pk=job.pk).status == "unknown"


def test_dispatcher_respects_future_work_and_live_leases(current):
    job = request(current)
    for values in ({"available_at": timezone.now() + timedelta(minutes=5)},
                   {"available_at": timezone.now(), "lease_until": timezone.now() + timedelta(minutes=5)}):
        DomainContactChange.objects.filter(pk=job.pk).update(**values)
        with patch("apps.domains.contact_change_jobs.publish_contact_change") as publisher:
            dispatch_contact_changes.run()
        publisher.assert_not_called()


def test_read_failure_retains_awaiting_state_and_backs_off(current):
    job = request(current)
    DomainContactChange.objects.filter(pk=job.pk).update(status="awaiting", action_id="701")
    with patch("apps.domains.resellerclub_client.ResellerClubClient"), patch(
        "apps.domains.contact_changes.observe_contact_change", side_effect=TimeoutError):
        run_contact_change.run(job.pk)
    job.refresh_from_db()
    assert job.status == "awaiting" and job.available_at > timezone.now() and job.lease_until is None


def test_queue_capacity_is_enforced_before_new_durable_work(current):
    with patch("apps.domains.contact_changes.MAX_OPEN_CHANGES", 0):
        with pytest.raises(ValidationError, match="queue is busy"):
            request(current)
    assert not DomainContactChange.objects.exists()
