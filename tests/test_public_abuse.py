"""SEC-06 foundation: early limits, atomic counters and typed public input."""
from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import MagicMock

import pytest
from django.core.cache import caches
from django.conf import settings
from django.test import RequestFactory, override_settings
from django.urls import reverse, resolve

from apps.core.abuse import consume_limit, AbuseLimitExceeded, POLICIES, PublicEndpointBoundaryMiddleware
from apps.products.models import Package
from apps.billing.models import Quote

pytestmark = pytest.mark.django_db


@pytest.fixture
def package():
    return Package.objects.create(name="Hosting", slug="public-abuse-hosting", price_monthly="10", price_annually="100", setup_fee="2", is_quotable=True)


def valid_payload(package):
    return {"lead": {"name": "Customer", "email": "customer@example.test"},
            "items": [{"package_id": package.pk, "description": "Hosting", "quantity": 1}], "hp": ""}


def test_concurrent_atomic_counters_admit_exactly_the_allowed_count():
    def attempt(index):
        try:
            consume_limit("concurrent-test", "client", 10, 60)
            return True
        except AbuseLimitExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(40))) == 10


def test_fixed_window_does_not_extend_on_each_attempt(monkeypatch):
    now = [120.0]
    monkeypatch.setattr("apps.core.abuse.time.time", lambda: now[0])
    consume_limit("window", "client", 1, 60)
    now[0] = 179.0
    with pytest.raises(AbuseLimitExceeded) as limited:
        consume_limit("window", "client", 1, 60)
    assert limited.value.retry_after == 1
    now[0] = 180.0
    consume_limit("window", "client", 1, 60)


def test_cache_keys_do_not_contain_raw_email_or_address(monkeypatch):
    backend = MagicMock()
    backend.incr.return_value = 1
    monkeypatch.setattr("apps.core.abuse.caches", {settings.PUBLIC_ABUSE_CACHE_ALIAS: backend})
    consume_limit("privacy", "private@example.test", 2, 60)
    key = backend.incr.call_args.args[0]
    assert "private@example.test" not in key
    backend.add.assert_called_once()


def test_contact_ip_limit_blocks_before_persistence_and_has_retry_after(client):
    from apps.core.models import ContactSubmission
    for index in range(5):
        assert client.post(reverse("core:contact"), {"email": f"different{index}@example.test"}).status_code == 200
    response = client.post(reverse("core:contact"), {"email": "new@example.test"})
    assert response.status_code == 429
    assert 1 <= int(response["Retry-After"]) <= 600
    assert not ContactSubmission.objects.exists()


def test_email_limit_applies_across_client_addresses(client):
    for index in range(3):
        assert client.post(reverse("account_reset_password"), {"email": "same@example.test"}, REMOTE_ADDR=f"192.0.2.{index + 1}").status_code == 302
    response = client.post(reverse("account_reset_password"), {"email": "SAME@example.test"}, REMOTE_ADDR="192.0.2.99")
    assert response.status_code == 429


def test_cache_failure_denies_work_without_revealing_exception(client, monkeypatch):
    backend = caches[settings.PUBLIC_ABUSE_CACHE_ALIAS]
    def unavailable(*args, **kwargs):
        raise RuntimeError("redis://private:secret@cache:6379")
    monkeypatch.setattr(backend, "incr", unavailable)
    response = client.post(reverse("core:contact"), {})
    assert response.status_code == 503
    assert b"private" not in response.content and b"secret" not in response.content


@pytest.mark.parametrize("name", ["core:contact", "accounts_custom:register", "account_reset_password", "accounts_custom:login", "billing_public:quote_submit"])
def test_oversized_public_request_rejected_before_view_work(client, name):
    response = client.post(reverse(name), data=b"x" * (70 * 1024), content_type="application/json")
    assert response.status_code == 413


def test_visible_body_limit_does_not_trust_false_length():
    request = RequestFactory().post(reverse("core:contact"), data=b"x" * 40000, content_type="application/x-www-form-urlencoded")
    request._body = b"x" * 40000
    request.META["CONTENT_LENGTH"] = "1"
    request.resolver_match = resolve(request.path)
    request.user = MagicMock(is_authenticated=False)
    response = PublicEndpointBoundaryMiddleware(lambda request: None).process_view(request, None, (), {})
    assert response.status_code == 413


@pytest.mark.parametrize("name", ["account_reset_password_from_key", "accounts_custom:mfa_verify"])
def test_completion_forms_are_bounded_before_authentication_work(client, name):
    # Use resolved route paths below so middleware runs before the view can
    # redirect an anonymous visitor or validate a reset token.
    kwargs = {"uidb36": "1", "key": "test-token"} if name == "account_reset_password_from_key" else {}
    url = reverse(name, kwargs=kwargs)
    response = client.post(url, b"x" * (33 * 1024), content_type="application/x-www-form-urlencoded")
    assert response.status_code == 413


@pytest.mark.parametrize("body", [b"[]", b"null", b'{"lead":1,"lead":2}', b'{"lead":NaN}', b"\xff",
                                  b'{"lead":{"email":"\\ud800"}}', b'{"items":[{"unit_price":1e999}]}'])
def test_malformed_quote_json_is_rejected_without_records(client, body):
    assert client.post(reverse("billing_public:quote_submit"), body, content_type="application/json").status_code == 400
    assert not Quote.objects.exists()


@pytest.mark.parametrize("quantity", [0, -1, 101, "NaN", "Infinity", "1e9", 1.5, True, {}, []])
def test_invalid_quote_quantities_do_not_create_documents(client, package, quantity):
    data = valid_payload(package)
    data["items"][0]["quantity"] = quantity
    response = client.post(reverse("billing_public:quote_submit"), json.dumps(data), content_type="application/json")
    assert response.status_code == 400
    assert not Quote.objects.exists()


@pytest.mark.parametrize("change", ["lead-list", "email-list", "name-long", "notes-long", "items-long", "kind-object", "unknown-item", "description-long"])
def test_typed_quote_fields_are_bounded(client, package, change):
    data = valid_payload(package)
    if change == "lead-list": data["lead"] = []
    elif change == "email-list": data["lead"]["email"] = []
    elif change == "name-long": data["lead"]["name"] = "x" * 201
    elif change == "notes-long": data["lead"]["notes"] = "x" * 4001
    elif change == "items-long": data["items"] *= 21
    elif change == "kind-object": data["items"][0]["kind"] = {}
    elif change == "unknown-item": data["items"][0]["package_id"] = 999999
    elif change == "description-long": data["items"][0]["description"] = "x" * 501
    assert client.post(reverse("billing_public:quote_submit"), json.dumps(data), content_type="application/json").status_code == 400
    assert not Quote.objects.exists()


def test_setup_word_cannot_select_a_cheaper_catalogue_price(client, package):
    data = valid_payload(package)
    data["items"][0]["description"] = "Please setup my hosting"
    data["items"][0]["unit_price"] = 0
    response = client.post(reverse("billing_public:quote_submit"), json.dumps(data), content_type="application/json")
    assert response.status_code == 200
    package.refresh_from_db()
    assert Quote.objects.get().line_items.get().unit_price == package.price_monthly


def test_public_quote_value_is_bounded_including_tax(client, package):
    package.price_monthly = "1000"
    package.save()
    data = valid_payload(package)
    data["items"][0]["quantity"] = 100
    assert client.post(reverse("billing_public:quote_submit"), json.dumps(data), content_type="application/json").status_code == 400
    assert not Quote.objects.exists()


def test_contact_html_mail_escapes_user_fields(client, mailoutbox):
    from apps.core.models import ContactFormSettings
    configuration = ContactFormSettings.get_solo()
    configuration.destination = ContactFormSettings.DESTINATION_EMAIL
    configuration.destination_email = "support@example.test"
    configuration.save()
    payload = "<script>alert(1)</script>"
    response = client.post(reverse("core:contact"), {"name": payload, "email": "contact@example.test", "subject": payload, "phone": payload, "message": payload})
    assert response.status_code == 200
    from apps.core.models import PublicDelivery
    from apps.core.tasks import deliver_public_submission
    assert not mailoutbox
    deliver_public_submission(str(PublicDelivery.objects.get().pk))
    assert len(mailoutbox) == 1
    html = mailoutbox[0].alternatives[0][0]
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_expensive_public_routes_are_in_the_inventory():
    assert {"core:contact", "billing_public:quote_submit", "accounts_custom:register", "account_signup", "account_reset_password", "domains:search", "domains:check", "domains:whois"} <= POLICIES.keys()


def test_custom_and_allauth_signup_share_the_email_budget(client):
    for name in ("accounts_custom:register", "account_signup", "accounts_custom:register"):
        assert client.post(reverse(name), {"email": "signup-shared@example.test"}).status_code == 200
    assert client.post(reverse("account_signup"), {"email": "SIGNUP-shared@example.test"}).status_code == 429


def test_shared_cache_requirement_is_checked_without_connecting_to_redis():
    from apps.core.checks import check_public_abuse_cache
    assert [error.id for error in check_public_abuse_cache(None)] == ["core.E002"]
    with override_settings(PUBLIC_ABUSE_CACHE_ALIAS="shared", CACHES={"shared": {"BACKEND": "django_redis.cache.RedisCache", "LOCATION": "redis://uncontacted.invalid:6379/0"}}):
        assert check_public_abuse_cache(None) == []


def test_oversized_login_password_is_rejected_before_hashing(client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Oversized password reached authentication")
    monkeypatch.setattr("apps.accounts.backends.EmailBackend.authenticate_password", forbidden)
    response = client.post(reverse("account_login"), {"email": "long-password@example.test", "password": "x" * 1025})
    assert response.status_code == 400
