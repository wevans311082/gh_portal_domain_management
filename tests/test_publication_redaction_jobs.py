from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.abuse import AbuseStoreUnavailable
from apps.domains.models import PublicationRedaction
from apps.domains.publication_redaction_jobs import publish_redaction, run_publication_redaction, dispatch_publication_redactions
from tests.test_publication_redaction import redaction
from tests.test_contact_change_admission import current
from tests.test_contact_change_submission import api
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


def test_eager_publish_never_runs_provider_work(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    with patch("apps.domains.publication_redaction_jobs.run_publication_redaction.delay") as sender:
        assert publish_redaction(1) is False
    sender.assert_not_called()


def test_worker_lease_stops_overlapping_delivery(redaction):
    job, api = redaction
    def process(identifier, *, client):
        assert PublicationRedaction.objects.get(pk=identifier).lease_until > timezone.now()
        run_publication_redaction.run(identifier)
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as factory, patch(
        "apps.domains.publication_redaction.process_publication_redaction", side_effect=process) as processor:
        run_publication_redaction.run(job.pk)
    assert factory.call_count == processor.call_count == 1
    assert PublicationRedaction.objects.get(pk=job.pk).lease_until is None


def test_limit_store_failure_defers_before_provider_creation(redaction):
    job, api = redaction
    with patch("apps.core.abuse.consume_limit", side_effect=AbuseStoreUnavailable), patch(
        "apps.domains.resellerclub_client.ResellerClubClient") as factory:
        run_publication_redaction.run(job.pk)
    factory.assert_not_called()
    assert PublicationRedaction.objects.get(pk=job.pk).available_at > timezone.now()


def test_unstable_source_is_reviewed_and_backed_off(redaction):
    job, api = redaction
    with patch("apps.domains.resellerclub_client.ResellerClubClient"), patch(
        "apps.domains.publication_redaction.process_publication_redaction", side_effect=ValidationError("pending source")):
        run_publication_redaction.run(job.pk)
    job.refresh_from_db()
    assert job.status == "review" and job.available_at > timezone.now()


def test_stale_submission_is_only_republished_as_uncertain_read(redaction):
    job, api = redaction
    PublicationRedaction.objects.filter(pk=job.pk).update(status="submitting", lease_until=timezone.now() - timedelta(seconds=1))
    with patch("apps.domains.publication_redaction_jobs.publish_redaction") as publisher:
        dispatch_publication_redactions.run()
    assert PublicationRedaction.objects.get(pk=job.pk).status == "unknown"
    publisher.assert_called_once_with(job.pk)
