"""Durable submission and delivery transitions without live email or brokers."""
from datetime import timedelta
import json
import uuid

import pytest
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Quote
from apps.core.models import PublicSubmission, PublicDelivery, ContactFormSettings, ContactSubmission
from apps.core.tasks import deliver_public_submission, dispatch_public_deliveries
from apps.products.models import Package

pytestmark = pytest.mark.django_db


@pytest.fixture
def package():
    return Package.objects.create(name="Delivery Plan", slug="delivery-plan", price_monthly="10", price_annually="100", is_quotable=True)


def quote_payload(package, key=None):
    data = {"lead": {"name": "Customer", "email": "delivery@example.test"},
            "items": [{"package_id": package.pk, "quantity": 1}]}
    if key is not None:
        data["submission_key"] = key
    return data


def post_quote(client, data):
    return client.post(reverse("billing_public:quote_submit"), json.dumps(data), content_type="application/json")


def queued_contact(client):
    settings = ContactFormSettings.get_solo()
    settings.destination = ContactFormSettings.DESTINATION_BOTH
    settings.destination_email = "support@example.test"
    settings.save()
    response = client.post(reverse("core:contact"), {"name": "Customer", "email": "contact@example.test", "message": "Please help"})
    assert response.status_code == 200
    return PublicDelivery.objects.get()


def test_repeated_quote_reuses_document_receipt_and_job(client, package, mailoutbox):
    data = quote_payload(package, str(uuid.uuid4()))
    first = post_quote(client, data)
    second = post_quote(client, data)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert Quote.objects.count() == PublicSubmission.objects.count() == PublicDelivery.objects.count() == 1
    assert not mailoutbox
    assert PublicDelivery.objects.get().state == "queued"


def test_legacy_quote_payload_also_deduplicates_within_session(client, package):
    first = post_quote(client, quote_payload(package))
    second = post_quote(client, quote_payload(package))
    assert first.json() == second.json()
    assert Quote.objects.count() == PublicDelivery.objects.count() == 1


def test_changed_payload_cannot_reuse_a_submission_key(client, package):
    data = quote_payload(package, str(uuid.uuid4()))
    assert post_quote(client, data).status_code == 200
    data["lead"]["name"] = "Changed"
    assert post_quote(client, data).status_code == 409
    assert Quote.objects.count() == 1


def test_other_browser_cannot_replay_private_quote_result(client, package):
    from django.test import Client
    data = quote_payload(package, str(uuid.uuid4()))
    response = post_quote(client, data)
    other = post_quote(Client(), data)
    assert other.status_code == 409
    assert response.json()["redirect"].encode() not in other.content
    assert Quote.objects.count() == 1


def test_signing_key_rotation_cannot_create_a_second_document_for_an_explicit_key(client, package, settings):
    data = quote_payload(package, str(uuid.uuid4()))
    assert post_quote(client, data).status_code == 200
    settings.SECRET_KEY_FALLBACKS = [settings.SECRET_KEY]
    settings.SECRET_KEY = "replacement-test-signing-key"
    assert post_quote(client, data).status_code == 409
    assert Quote.objects.count() == PublicSubmission.objects.count() == 1


@pytest.mark.parametrize("key", ["not-a-uuid", {}, 123, "x" * 1000])
def test_invalid_submission_keys_do_not_create_documents(client, package, key):
    assert post_quote(client, quote_payload(package, key)).status_code == 409
    assert not Quote.objects.exists()
    assert not PublicSubmission.objects.exists()


def test_contact_repeats_do_not_duplicate_database_or_email_work(client, mailoutbox):
    job = queued_contact(client)
    response = client.post(reverse("core:contact"), {"name": "Customer", "email": "contact@example.test", "message": "Please help"})
    assert response.status_code == 200
    assert ContactSubmission.objects.count() == PublicDelivery.objects.count() == 1
    assert not mailoutbox
    assert deliver_public_submission(str(job.pk)) == "sent"
    assert deliver_public_submission(str(job.pk)) == "skipped"
    assert len(mailoutbox) == 1
    job.refresh_from_db()
    assert job.state == "sent" and job.data == {} and job.completed_at
    assert mailoutbox[0].extra_headers["Message-ID"] == f"<public-{job.pk}@portal.invalid>"


def test_email_only_contact_retains_no_admin_contact_record(client, mailoutbox):
    configuration = ContactFormSettings.get_solo()
    configuration.destination = ContactFormSettings.DESTINATION_EMAIL
    configuration.destination_email = "support@example.test"
    configuration.save()
    client.post(reverse("core:contact"), {"name": "Customer", "email": "contact@example.test", "message": "Private enquiry"})
    assert not ContactSubmission.objects.exists()
    job = PublicDelivery.objects.get()
    assert deliver_public_submission(str(job.pk)) == "sent"
    job.refresh_from_db()
    assert job.data == {} and len(mailoutbox) == 1


def test_persistence_failure_rolls_back_receipt_quote_and_job(client, package, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("Outbox unavailable")
    monkeypatch.setattr(PublicDelivery.objects, "create", unavailable)
    with pytest.raises(RuntimeError, match="Outbox unavailable"):
        post_quote(client, quote_payload(package))
    assert not Quote.objects.exists()
    assert not PublicSubmission.objects.exists()


def test_broker_failure_keeps_a_recoverable_queued_job(client, package, monkeypatch, django_capture_on_commit_callbacks):
    from django.test import override_settings
    def unavailable(*args, **kwargs):
        raise RuntimeError("Broker unavailable")
    monkeypatch.setattr(deliver_public_submission, "apply_async", unavailable)
    with override_settings(CELERY_TASK_ALWAYS_EAGER=False):
        with django_capture_on_commit_callbacks(execute=True):
            assert post_quote(client, quote_payload(package)).status_code == 200
    assert PublicDelivery.objects.get().state == "queued"
    published = []
    monkeypatch.setattr("apps.core.public_submissions.publish_delivery", lambda identifier: published.append(identifier))
    assert dispatch_public_deliveries() == 1
    assert published == [PublicDelivery.objects.get().pk]


def test_eager_configuration_cannot_run_delivery_in_the_http_request(client, package, monkeypatch, django_capture_on_commit_callbacks, mailoutbox):
    monkeypatch.setattr(deliver_public_submission, "apply_async", lambda *args, **kwargs: pytest.fail("Eager execution was allowed"))
    with django_capture_on_commit_callbacks(execute=True):
        assert post_quote(client, quote_payload(package)).status_code == 200
    assert not mailoutbox
    assert PublicDelivery.objects.get().state == "queued"


def test_preparation_retries_are_bounded_and_never_send(client, monkeypatch, mailoutbox):
    job = queued_contact(client)
    def broken_prepare(*args):
        raise RuntimeError("Private preparation exception")
    monkeypatch.setattr("apps.core.tasks.prepare_public_message", broken_prepare)
    for attempt, result in enumerate(("retry", "retry", "failed"), start=1):
        PublicDelivery.objects.filter(pk=job.pk).update(available_at=timezone.now() - timedelta(seconds=1))
        assert deliver_public_submission(str(job.pk)) == result
        job.refresh_from_db()
        assert job.attempts == attempt
        assert "Private" not in job.error_code
    assert deliver_public_submission(str(job.pk)) == "skipped"
    assert not mailoutbox


def test_ambiguous_delivery_is_not_automatically_repeated(client, monkeypatch):
    job = queued_contact(client)
    calls = []
    def uncertain_send(*args, **kwargs):
        calls.append("possibly-sent")
        raise TimeoutError("SMTP outcome unknown")
    monkeypatch.setattr(EmailMultiAlternatives, "send", uncertain_send)
    assert deliver_public_submission(str(job.pk)) == "unknown"
    assert deliver_public_submission(str(job.pk)) == "skipped"
    job.refresh_from_db()
    assert job.state == "unknown" and job.error_code == "delivery_uncertain"
    assert calls == ["possibly-sent"]


def test_interrupted_delivery_requires_review_and_is_never_requeued(client, monkeypatch):
    job = queued_contact(client)
    PublicDelivery.objects.filter(pk=job.pk).update(state="delivering", claimed_at=timezone.now() - timedelta(minutes=5))
    monkeypatch.setattr("apps.core.public_submissions.publish_delivery", lambda identifier: pytest.fail("Uncertain job was republished"))
    assert dispatch_public_deliveries() == 0
    job.refresh_from_db()
    assert job.state == "unknown"


def test_stale_preparation_can_recover_but_old_worker_cannot_send(client, monkeypatch, mailoutbox):
    job = queued_contact(client)
    def lose_lease(current):
        PublicDelivery.objects.filter(pk=current.pk).update(state="queued", lease_token=None)
        return EmailMultiAlternatives(subject="prepared", body="body", to=["support@example.test"])
    monkeypatch.setattr("apps.core.tasks.prepare_public_message", lose_lease)
    assert deliver_public_submission(str(job.pk)) == "lease_lost"
    assert not mailoutbox


def test_full_queue_does_not_create_another_quote_and_replay_still_works(client, package, monkeypatch):
    data = quote_payload(package, str(uuid.uuid4()))
    first = post_quote(client, data)
    monkeypatch.setattr("apps.core.public_submissions.MAX_PENDING_DELIVERIES", 1)
    assert post_quote(client, data).json() == first.json()
    data["submission_key"] = str(uuid.uuid4())
    assert post_quote(client, data).status_code == 503
    assert Quote.objects.count() == PublicDelivery.objects.count() == PublicSubmission.objects.count() == 1


def test_separate_gets_supply_new_keys_and_owner_marker(client):
    first = client.get(reverse("core:contact"))
    second = client.get(reverse("core:contact"))
    assert first.context["form"].initial["submission_key"] != second.context["form"].initial["submission_key"]
    assert client.session["_public_submission_owner"]
    assert "no-store" in first["Cache-Control"]


def test_owner_can_review_uncertain_delivery_without_resending(client, django_user_model, mailoutbox):
    from tests.security_helpers import login_owner
    from apps.audit.models import AuditLog
    job = queued_contact(client)
    PublicDelivery.objects.filter(pk=job.pk).update(state="unknown")
    owner = django_user_model.objects.create_user(email="delivery-review-owner@example.test")
    login_owner(client, owner)
    response = client.post(reverse("admin_tools:task_management"), {
        "job": str(job.pk), "action": "confirm_sent", "reason": "Provider event confirms this message ID was accepted",
    })
    assert response.status_code == 302
    job.refresh_from_db()
    assert job.state == "sent" and job.data == {}
    assert not mailoutbox
    assert AuditLog.objects.get(action="public.delivery.reviewed").user == owner


def test_owner_can_find_queued_deliveries_and_close_them_safely(client, django_user_model, mailoutbox):
    from tests.security_helpers import login_owner
    job = queued_contact(client)
    login_owner(client, django_user_model.objects.create_user(email="delivery-queue-owner@example.test"))
    response = client.get(reverse("admin_tools:task_management"), {"public_state": "queued"})
    assert str(job.pk).encode() in response.content
    response = client.post(reverse("admin_tools:task_management"), {"job": str(job.pk), "action": "cancel", "reason": "Withdrawn enquiry"})
    assert response.status_code == 302
    assert deliver_public_submission(str(job.pk)) == "skipped"
    job.refresh_from_db()
    assert job.state == "cancelled" and job.data == {}
    assert not mailoutbox


def test_owner_cannot_retry_uncertain_send_but_can_retry_failed_preparation(client, django_user_model):
    from tests.security_helpers import login_owner
    job = queued_contact(client)
    PublicDelivery.objects.filter(pk=job.pk).update(state="unknown", attempts=3, total_attempts=3)
    login_owner(client, django_user_model.objects.create_user(email="delivery-retry-owner@example.test"))
    url = reverse("admin_tools:task_management")
    data = {"job": str(job.pk), "action": "retry_preparation", "reason": "Investigate delivery"}
    response = client.post(url, data)
    assert response.status_code == 200
    assert b"This action is not allowed" in response.content
    assert b'aria-invalid="true"' in response.content
    job.refresh_from_db()
    assert job.state == "unknown"
    PublicDelivery.objects.filter(pk=job.pk).update(state="failed")
    assert client.post(url, data).status_code == 302
    job.refresh_from_db()
    assert job.state == "queued" and job.attempts == 0 and job.total_attempts == 3


def test_delivery_review_is_atomic_with_mandatory_audit(client, django_user_model, monkeypatch):
    from tests.security_helpers import login_owner
    from apps.audit.models import AuditLog
    job = queued_contact(client)
    PublicDelivery.objects.filter(pk=job.pk).update(state="unknown")
    login_owner(client, django_user_model.objects.create_user(email="delivery-audit-owner@example.test"))
    def unavailable(*args, **kwargs):
        raise RuntimeError("Audit unavailable")
    monkeypatch.setattr(AuditLog.objects, "create", unavailable)
    with pytest.raises(RuntimeError, match="Audit unavailable"):
        client.post(reverse("admin_tools:task_management"), {"job": str(job.pk), "action": "cancel", "reason": "Close"})
    job.refresh_from_db()
    assert job.state == "unknown" and job.data


def test_public_graph_timeout_never_falls_back_to_another_transport(monkeypatch):
    from apps.notifications.email_backend import MicrosoftGraphEmailBackend
    from apps.notifications.m365_graph import MicrosoftGraphMailConfig, MicrosoftGraphMailClient, MicrosoftGraphMailError
    config = MicrosoftGraphMailConfig(enabled=True, tenant_id="tenant", client_id="client", client_secret="secret",
        default_mailbox="support@example.test", billing_mailbox="", support_mailbox="", domains_mailbox="",
        notifications_mailbox="", save_to_sent_items=True, timeout_seconds=15)
    monkeypatch.setattr(MicrosoftGraphMailConfig, "load", lambda: config)
    timeouts = []
    def uncertain(client, *args, **kwargs):
        timeouts.append(client.config.timeout_seconds)
        raise TimeoutError("Possibly accepted")
    monkeypatch.setattr(MicrosoftGraphMailClient, "send_mail", uncertain)
    backend = MicrosoftGraphEmailBackend()
    monkeypatch.setattr(backend, "_safe_fallback_send", lambda *args: pytest.fail("A second transport was attempted"))
    message = EmailMultiAlternatives(subject="Test", body="body", to=["recipient@example.test"], headers={"X-CyberAsk-No-Fallback": "1"})
    with pytest.raises(MicrosoftGraphMailError, match="requires review"):
        backend.send_messages([message])
    assert timeouts == [10]


@pytest.mark.django_db(transaction=True)
def test_concurrent_receipts_and_safe_retries_do_not_duplicate_records(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.contrib.auth.models import AnonymousUser
    from django.db import close_old_connections
    from django.test import RequestFactory
    from apps.core.public_submissions import accept_submission, SubmissionUnavailable
    barrier = Barrier(2)
    key = str(uuid.uuid4())
    marker = uuid.uuid4().hex
    def submit(wait=False):
        request = RequestFactory().post("/contact/")
        request.user = AnonymousUser()
        request.session = {"_public_submission_owner": marker}
        def create():
            record = ContactSubmission.objects.create(name="Concurrent", email="concurrent@example.test", message="Once")
            return {"contact_id": record.pk}, None
        if wait:
            barrier.wait(timeout=10)
        try:
            return accept_submission(request, "contact", {"message": "Once"}, create, key=key, requires_delivery=False)[0]
        except SubmissionUnavailable:
            return None  # SQLite writer contention is an explicit safe retry.
    def worker():
        close_old_connections()
        try:
            return submit(wait=True)
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: worker(), range(2)))
    final = submit()
    assert final is not None
    assert all(result is None or result == final for result in results)
    assert ContactSubmission.objects.count() == PublicSubmission.objects.count() == 1
