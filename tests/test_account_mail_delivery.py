import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.mail_delivery import queue_account_mail
from apps.core.models import PublicDelivery, PublicSubmission
from apps.core.public_submissions import SubmissionUnavailable
from apps.core.tasks import deliver_public_submission

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(email="queued@example.test", password="test-password")


def message():
    msg = EmailMultiAlternatives("Reset your password", "Secret link: https://portal.test/reset/token-value", "from@example.test", ["queued@example.test"])
    msg.attach_alternative("<p>Secret token-value</p>", "text/html")
    return msg


def test_web_reset_queues_encrypted_mail_and_worker_delivers(client, user):
    with patch("django.core.mail.message.EmailMessage.send", side_effect=AssertionError("HTTP must not send")):
        response = client.post(reverse("account_reset_password"), {"email": user.email})
    assert response.status_code == 302 and mail.outbox == []
    job = PublicDelivery.objects.select_related("submission").get()
    assert job.submission.kind == "account" and job.data["envelope"].startswith("enc:v1:")
    assert user.email not in json.dumps(job.data)
    assert deliver_public_submission(str(job.pk)) == "sent"
    assert len(mail.outbox) == 1
    assert mail.outbox[0].extra_headers["X-CyberAsk-No-Fallback"] == "1"
    job.refresh_from_db()
    assert job.data == {}


def test_identical_mail_deduplicates_but_later_resend_is_available(user):
    now = timezone.now().replace(second=0, microsecond=0)
    with patch("apps.accounts.mail_delivery.timezone.now", return_value=now):
        first = queue_account_mail(message(), "account/email/password_reset_key", user)
        assert queue_account_mail(message(), "account/email/password_reset_key", user) == first
    with patch("apps.accounts.mail_delivery.timezone.now", return_value=now + timedelta(seconds=61)):
        assert queue_account_mail(message(), "account/email/password_reset_key", user) == first
    assert deliver_public_submission(str(first)) == "sent"
    with patch("apps.accounts.mail_delivery.timezone.now", return_value=now + timedelta(seconds=61)):
        assert queue_account_mail(message(), "account/email/password_reset_key", user) != first
    assert PublicSubmission.objects.count() == PublicDelivery.objects.count() == 2


@pytest.mark.parametrize("change", ["expired", "password", "inactive", "deleted", "address"])
def test_stale_or_ineligible_account_mail_is_cancelled(user, change):
    identifier = queue_account_mail(message(), "account/email/password_reset_key", user)
    if change == "password":
        user.set_password("new-password")
        user.save(update_fields=["password"])
    elif change == "inactive":
        user.is_active = False
        user.save(update_fields=["is_active"])
    elif change == "deleted":
        user.delete()
    elif change == "address":
        user.email = "changed@example.test"
        user.save(update_fields=["email"])
    if change == "expired":
        with patch("apps.accounts.mail_delivery.timezone.now", return_value=timezone.now() + timedelta(seconds=901)):
            assert deliver_public_submission(str(identifier)) == "cancelled"
    else:
        assert deliver_public_submission(str(identifier)) == "cancelled"
    assert mail.outbox == []
    assert PublicDelivery.objects.get(pk=identifier).data == {}


def test_ciphertext_cannot_be_moved_to_another_job(user):
    first = queue_account_mail(message(), "account/email/password_reset_key", user)
    other = message()
    other.body = "Different secret"
    second = queue_account_mail(other, "account/email/password_reset_key", user)
    packet = PublicDelivery.objects.get(pk=first).data
    PublicDelivery.objects.filter(pk=second).update(data=packet)
    assert deliver_public_submission(str(second)) == "retry"
    assert mail.outbox == []


@pytest.mark.parametrize("failure", ["capacity", "keys"])
def test_admission_failure_leaves_no_receipt_or_job(user, failure, monkeypatch):
    if failure == "capacity":
        monkeypatch.setattr("apps.core.public_submissions.MAX_PENDING_DELIVERIES", 0)
    else:
        monkeypatch.setattr(settings, "INTEGRATION_ENCRYPTION_KEYS", {})
    with pytest.raises(SubmissionUnavailable):
        queue_account_mail(message(), "account/email/password_reset_key", user)
    assert not PublicSubmission.objects.exists() and not PublicDelivery.objects.exists()


@pytest.mark.parametrize("invalid", ["size", "attachment", "recipients"])
def test_message_budgets_reject_before_storage(user, invalid):
    msg = message()
    if invalid == "size": msg.body = "x" * (256 * 1024)
    elif invalid == "attachment": msg.attach("file.txt", b"secret", "text/plain")
    else: msg.to.append("another@example.test")
    with pytest.raises(SubmissionUnavailable):
        queue_account_mail(msg, "account/email/password_reset_key", user)
    assert not PublicDelivery.objects.exists()


@override_settings(INTEGRATION_ENCRYPTION_KEYS={}, INTEGRATION_ENCRYPTION_ACTIVE_KEY="")
def test_http_reset_key_failure_is_generic_and_has_no_synchronous_fallback(client, user):
    response = client.post(reverse("account_reset_password"), {"email": user.email}, HTTP_ACCEPT="text/html")
    assert response.status_code == 503
    assert b"temporarily unavailable" in response.content
    assert user.email.encode() not in response.content and b"encryption" not in response.content
    assert mail.outbox == [] and not PublicDelivery.objects.exists()


def test_worker_send_uncertainty_is_not_retried(user):
    identifier = queue_account_mail(message(), "account/email/password_reset_key", user)
    with patch("django.core.mail.message.EmailMessage.send", side_effect=TimeoutError("private-token")):
        assert deliver_public_submission(str(identifier)) == "unknown"
    assert deliver_public_submission(str(identifier)) == "skipped"
    assert PublicDelivery.objects.get(pk=identifier).error_code == "delivery_uncertain"


def test_key_rotation_reads_existing_envelopes_while_old_key_is_retained(user):
    identifier = queue_account_mail(message(), "account/email/password_reset_key", user)
    keys = dict(settings.INTEGRATION_ENCRYPTION_KEYS)
    keys["mail-v2"] = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    with override_settings(INTEGRATION_ENCRYPTION_KEYS=keys, INTEGRATION_ENCRYPTION_ACTIVE_KEY="mail-v2"):
        assert deliver_public_submission(str(identifier)) == "sent"
    assert "token-value" in mail.outbox[0].body


def test_missing_old_key_never_falls_back_to_plaintext_or_transport(user):
    identifier = queue_account_mail(message(), "account/email/password_reset_key", user)
    with override_settings(INTEGRATION_ENCRYPTION_KEYS={"new": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}, INTEGRATION_ENCRYPTION_ACTIVE_KEY="new"):
        assert deliver_public_submission(str(identifier)) == "retry"
    assert mail.outbox == []


def test_ambiguous_legacy_case_variants_do_not_fan_out_reset_mail(client, django_user_model):
    for address in ("Duplicate@example.test", "duplicate@example.test", "DUPLICATE@example.test"):
        django_user_model.objects.create_user(email=address, password="test-password")
    response = client.post(reverse("account_reset_password"), {"email": "duplicate@example.test"})
    assert response.status_code == 200
    assert b"Please contact support" in response.content
    assert not PublicDelivery.objects.exists() and mail.outbox == []


@override_settings(INTEGRATION_ENCRYPTION_KEYS={}, INTEGRATION_ENCRYPTION_ACTIVE_KEY="")
def test_registration_reports_queue_failure_without_raw_exception(client, django_user_model):
    response = client.post(reverse("accounts_custom:register"), {"email": "newqueued@example.test", "first_name": "New", "last_name": "Customer", "phone": "",
                          "password1": "SomeStrongPassword123!", "password2": "SomeStrongPassword123!"}, follow=True)
    assert response.status_code == 200
    assert b"account was created, but its confirmation email could not be queued" in response.content
    assert b"encryption keys" not in response.content
    assert django_user_model.objects.filter(email="newqueued@example.test").exists()
    assert not PublicDelivery.objects.exists() and mail.outbox == []


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_mail_and_safe_retries_create_one_job(user, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import close_old_connections
    monkeypatch.setattr("apps.accounts.mail_delivery.publish_delivery", lambda identifier: None)
    barrier = Barrier(2)
    now = timezone.now().replace(second=0, microsecond=0)
    def submit(wait=False):
        if wait:
            barrier.wait(timeout=10)
        try:
            return queue_account_mail(message(), "account/email/password_reset_key", user)
        except SubmissionUnavailable:
            return None
    def worker():
        close_old_connections()
        try:
            return submit(True)
        finally:
            close_old_connections()
    with patch("apps.accounts.mail_delivery.timezone.now", return_value=now), ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: worker(), range(2)))
        final = submit()
    assert final is not None and all(identifier is None or identifier == final for identifier in results)
    assert PublicDelivery.objects.count() == PublicSubmission.objects.count() == 1
