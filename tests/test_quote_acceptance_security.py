from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Invoice, Quote
from apps.billing.services import LineItemSpec, create_quote
from apps.core.models import PublicDelivery, PublicSubmission

pytestmark = pytest.mark.django_db


@pytest.fixture
def account(django_user_model):
    return django_user_model.objects.create_user(email="accept@example.test", password="test-password")


@pytest.fixture
def quote():
    return create_quote(user=None, line_items=[LineItemSpec(description="Hosting", quantity=Decimal(1), unit_price=Decimal(10))],
                        lead_email="lead@example.test", status=Quote.STATUS_SENT)


def url(quote, continuation=False):
    return reverse("billing_public:quote_public_accept_continue" if continuation else "billing_public:quote_public_accept", args=[quote.public_token])


def test_get_after_login_is_read_only_confirmation(client, account, quote):
    client.force_login(account)
    response = client.get(url(quote, True))
    assert response.status_code == 200
    assert b"Accept quote and create invoice" in response.content
    assert b"csrfmiddlewaretoken" in response.content
    assert response["Cache-Control"].find("no-store") >= 0
    quote.refresh_from_db()
    assert quote.user_id is None and quote.accepted_at is None and quote.status == Quote.STATUS_SENT
    assert not Invoice.objects.exists() and not PublicDelivery.objects.exists()


@pytest.mark.parametrize("continuation", [False, True])
def test_real_csrf_required_and_valid_confirmation_works(account, quote, continuation):
    client = Client(enforce_csrf_checks=True)
    client.force_login(account)
    assert client.post(url(quote, continuation)).status_code == 403
    assert not Invoice.objects.exists()
    client.get(url(quote, True))
    token = client.cookies["csrftoken"].value
    response = client.post(url(quote, continuation), {"csrfmiddlewaretoken": token})
    assert response.status_code == 302
    assert Invoice.objects.filter(user=account, source_quote=quote).count() == 1


@pytest.mark.parametrize("continuation", [False, True])
def test_foreign_account_cannot_accept_or_discover_existing_invoice(client, account, quote, django_user_model, continuation):
    other = django_user_model.objects.create_user(email="otheraccept@example.test", password="test-password")
    quote.user = other
    quote.save(update_fields=["user"])
    client.force_login(account)
    response = client.post(url(quote, continuation))
    assert response.status_code == 403
    assert not Invoice.objects.exists() and not PublicDelivery.objects.exists()
    assert client.get(url(quote, True)).status_code == 403


def test_replays_across_routes_and_sessions_return_one_invoice_and_job(client, account, quote):
    client.force_login(account)
    with patch("apps.billing.services.email_document") as email, patch("apps.core.public_submissions.publish_delivery") as publish:
        first = client.post(url(quote))
        client.logout()
        client.force_login(account)
        second = client.post(url(quote, True))
    assert first.status_code == second.status_code == 302
    assert first.url == second.url
    assert Invoice.objects.filter(source_quote=quote).count() == 1
    assert PublicDelivery.objects.count() == PublicSubmission.objects.filter(kind="invoice").count() == 1
    email.assert_not_called()
    # Test transactions do not commit the callback; the durable queue exists.
    publish.assert_not_called()
    quote.refresh_from_db()
    assert quote.user == account and quote.accepted_at and quote.converted_invoice_id


@pytest.mark.parametrize("invalid", ["expired", "void", "draft"])
def test_unacceptable_quote_is_unchanged(client, account, quote, invalid):
    if invalid == "expired":
        quote.valid_until = timezone.now().date() - timedelta(days=1)
    else:
        quote.status = invalid
    quote.save()
    client.force_login(account)
    response = client.post(url(quote, True))
    assert response.status_code == 302
    quote.refresh_from_db()
    assert quote.user_id is None and quote.accepted_at is None
    assert not Invoice.objects.exists() and not PublicDelivery.objects.exists()


def test_conversion_failure_rolls_back_claim_and_receipt(client, account, quote):
    client.force_login(account)
    from apps.core.public_submissions import SubmissionUnavailable
    with patch("apps.billing.acceptance.convert_quote_to_invoice", side_effect=SubmissionUnavailable("private details")):
        response = client.post(url(quote, True))
    assert response.status_code == 503 and b"private details" not in response.content
    assert response["Retry-After"] == "60"
    quote.refresh_from_db()
    assert quote.user_id is None and quote.status == Quote.STATUS_SENT and quote.accepted_at is None
    assert not PublicSubmission.objects.exists() and not PublicDelivery.objects.exists() and not Invoice.objects.exists()


def test_invoice_delivery_uses_preparation_only_and_normal_worker_boundary(client, account, quote):
    client.force_login(account)
    client.post(url(quote))
    from apps.core.tasks import prepare_public_message, deliver_public_submission
    from django.core.mail import EmailMultiAlternatives
    job = PublicDelivery.objects.select_related("submission").get()
    with patch("apps.billing.services.email_document", return_value=EmailMultiAlternatives("Invoice", "Ready", to=[account.email])) as email:
        assert prepare_public_message(job).to == [account.email]
        email.assert_called_once_with(Invoice.objects.get(source_quote=quote), kind="invoice_issued", prepare_only=True)
        assert deliver_public_submission(str(job.pk)) == "sent"
    job.refresh_from_db()
    assert job.state == "sent" and job.data == {}


def test_acceptance_aliases_share_rate_budget_and_reject_oversized_body(client, account, quote):
    client.force_login(account)
    assert client.post(url(quote), b"x" * 40000, content_type="application/x-www-form-urlencoded").status_code == 413
    for index in range(9):
        assert client.post(url(quote, bool(index % 2))).status_code == 302
    assert client.post(url(quote, True)).status_code == 429
    assert Invoice.objects.filter(source_quote=quote).count() == PublicDelivery.objects.count() == 1


def test_capacity_failure_leaves_quote_unclaimed(client, account, quote, monkeypatch):
    monkeypatch.setattr("apps.core.public_submissions.MAX_PENDING_DELIVERIES", 0)
    client.force_login(account)
    response = client.post(url(quote))
    assert response.status_code == 503
    quote.refresh_from_db()
    assert quote.user_id is None and quote.accepted_at is None and quote.status == Quote.STATUS_SENT
    assert not Invoice.objects.exists() and not PublicSubmission.objects.exists()


def test_other_account_cannot_replay_converted_quote(client, account, quote, django_user_model):
    client.force_login(account)
    client.post(url(quote))
    other = django_user_model.objects.create_user(email="foreign-replay@example.test", password="test-password")
    client.force_login(other)
    assert client.post(url(quote, True)).status_code == 403
    assert client.get(url(quote, True)).status_code == 403
    assert Invoice.objects.count() == PublicDelivery.objects.count() == 1


def test_stale_service_instance_reuses_existing_conversion(account, quote):
    from apps.billing.services import convert_quote_to_invoice
    quote.user = account
    quote.save(update_fields=["user"])
    stale = Quote.objects.get(pk=quote.pk)
    first = convert_quote_to_invoice(quote, send_email=False)
    second = convert_quote_to_invoice(stale, send_email=False)
    assert first.pk == second.pk
    assert Invoice.objects.filter(source_quote=quote).count() == 1


def test_unsupported_methods_do_not_accept_or_mutate_quote(client, account, quote):
    client.force_login(account)
    assert client.get(url(quote)).status_code == 405
    assert client.put(url(quote, True)).status_code == 405
    assert client.post(reverse("billing_public:quote_public", args=[quote.public_token])).status_code == 405
    quote.refresh_from_db()
    assert quote.user_id is None and quote.accepted_at is None and quote.status == Quote.STATUS_SENT
    assert not Invoice.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_acceptance_safe_retries_create_one_invoice(account, quote, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import close_old_connections
    from django.test import RequestFactory
    from apps.billing.acceptance import accept_public_quote
    from apps.core.public_submissions import SubmissionUnavailable
    monkeypatch.setattr("apps.core.public_submissions.publish_delivery", lambda identifier: None)
    barrier = Barrier(2)
    def submit(wait=False):
        request = RequestFactory().post(url(quote))
        request.user = account
        request.session = {}
        if wait:
            barrier.wait(timeout=10)
        try:
            return accept_public_quote(request, quote.public_token).pk
        except SubmissionUnavailable:
            return None
    def worker():
        close_old_connections()
        try:
            return submit(True)
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: worker(), range(2)))
    final = submit()
    assert final is not None and all(value is None or value == final for value in results)
    assert Invoice.objects.filter(source_quote=quote).count() == PublicDelivery.objects.count() == 1
