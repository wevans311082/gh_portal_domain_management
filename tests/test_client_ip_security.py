"""SEC-04: socket trust, forwarding-chain validation and shared consumers."""
import pytest
from django.test import RequestFactory, override_settings
from django.http import HttpResponse
from django.urls import reverse

from apps.core.http import get_client_ip
from apps.core.checks import check_trusted_proxies
from apps.accounts.views import _login_rate_key
from apps.domains.views import _rate_limit_key
from apps.billing.public_views import _client_ip
from apps.audit.middleware import _get_client_ip, IPAllowlistMiddleware, AuditLogMiddleware
from apps.audit.models import IPAllowlistEntry, AuditLog


def request_for(peer, forwarded=None):
    meta = {"REMOTE_ADDR": peer}
    if forwarded is not None:
        meta["HTTP_X_FORWARDED_FOR"] = forwarded
    return RequestFactory().get("/", **meta)


@override_settings(TRUSTED_PROXY_CIDRS=[])
@pytest.mark.parametrize("peer,expected", [
    ("192.0.2.20", "192.0.2.20"), ("2001:0db8:0:0::20", "2001:db8::20"),
    ("::ffff:192.0.2.20", "192.0.2.20"), ("", ""), ("not-an-ip", ""),
    ("192.0.2.20:1234", ""), ("fe80::1%eth0", ""),
])
def test_untrusted_socket_never_uses_forwarding_headers(peer, expected):
    request = request_for(peer, "198.51.100.5, 10.0.0.8")
    request.META["HTTP_X_REAL_IP"] = "198.51.100.6"
    request.META["HTTP_FORWARDED"] = 'for="198.51.100.7"'
    assert get_client_ip(request) == expected


@override_settings(TRUSTED_PROXY_CIDRS=["10.0.0.8/32", "10.0.0.9/32", "2001:db8:1::/64"])
@pytest.mark.parametrize("peer,header,expected", [
    ("10.0.0.8", None, "10.0.0.8"),
    ("10.0.0.8", "198.51.100.5", "198.51.100.5"),
    ("10.0.0.8", "203.0.113.99, 198.51.100.5", "198.51.100.5"),
    ("10.0.0.8", "203.0.113.99, 198.51.100.5, 10.0.0.9", "198.51.100.5"),
    ("10.0.0.8", "198.51.100.5, 10.0.0.7", "10.0.0.7"),
    ("10.0.0.8", "10.0.0.9", "10.0.0.9"),
    ("2001:db8:1::1", "2001:0db8:2::9, 2001:db8:1::2", "2001:db8:2::9"),
    ("::ffff:10.0.0.8", "::ffff:198.51.100.5", "198.51.100.5"),
])
def test_trusted_chain_stops_at_first_untrusted_hop(peer, header, expected):
    assert get_client_ip(request_for(peer, header)) == expected


@override_settings(TRUSTED_PROXY_CIDRS=["10.0.0.8"])
@pytest.mark.parametrize("header", [
    "unknown", "198.51.100.5,", ",198.51.100.5", "198.51.100.5,,10.0.0.9",
    "198.51.100.5:123", "[2001:db8::1]", "fe80::1%eth0", "198.51.100.5\r\nInjected: yes",
    "for=198.51.100.5", "garbage,198.51.100.5", "a" * 4097,
    ",".join(["198.51.100.5"] * 33),
])
def test_malformed_trusted_header_fails_closed(header):
    assert get_client_ip(request_for("10.0.0.8", header)) == ""


@pytest.mark.parametrize("configuration", ["10.0.0.8", ["10.0.0.8/24"], ["bad"], [None], ["fe80::1%eth0"]])
def test_invalid_configuration_is_reported_and_fails_closed(configuration):
    with override_settings(TRUSTED_PROXY_CIDRS=configuration):
        assert [error.id for error in check_trusted_proxies(None)] == ["core.E001"]
        assert get_client_ip(request_for("192.0.2.20")) == ""


@override_settings(TRUSTED_PROXY_CIDRS=["10.0.0.8"])
def test_all_security_consumers_share_one_address():
    request = request_for("10.0.0.8", "203.0.113.99, 198.51.100.5")
    assert _login_rate_key(request) == "login_rl:198.51.100.5"
    assert _rate_limit_key(request) == "domain_check_rl:198.51.100.5"
    assert _client_ip(request) == _get_client_ip(request) == "198.51.100.5"


@pytest.mark.django_db
@override_settings(TRUSTED_PROXY_CIDRS=["10.0.0.8"])
def test_staff_allowlist_cannot_be_spoofed_by_direct_caller(django_user_model):
    user = django_user_model.objects.create_user(email="ip-staff@example.test", is_staff=True)
    IPAllowlistEntry.objects.create(ip_address="198.51.100.5", is_active=True)
    middleware = IPAllowlistMiddleware(lambda request: HttpResponse("allowed"))
    direct = request_for("192.0.2.20", "198.51.100.5")
    direct.user = user
    assert middleware(direct).status_code == 403
    proxied = request_for("10.0.0.8", "198.51.100.5")
    proxied.user = user
    assert middleware(proxied).status_code == 200
    malformed = request_for("10.0.0.8", "invalid,198.51.100.5")
    malformed.user = user
    assert middleware(malformed).status_code == 403


@pytest.mark.django_db
@override_settings(TRUSTED_PROXY_CIDRS=["10.0.0.8"])
def test_audit_records_use_canonical_verified_address(django_user_model):
    user = django_user_model.objects.create_user(email="ip-audit@example.test")
    request = RequestFactory().post("/test-address/", REMOTE_ADDR="10.0.0.8", HTTP_X_FORWARDED_FOR="203.0.113.99, 198.51.100.5")
    request.user = user
    AuditLogMiddleware(lambda request: HttpResponse("ok"))(request)
    assert AuditLog.objects.get(action="POST /test-address/").ip_address == "198.51.100.5"


@pytest.mark.django_db
@override_settings(TRUSTED_PROXY_CIDRS=[])
def test_contact_submission_ignores_forged_forwarded_address(client):
    from apps.core.models import ContactFormSettings, ContactSubmission
    configuration = ContactFormSettings.get_solo()
    configuration.destination = ContactFormSettings.DESTINATION_DB
    configuration.save()
    response = client.post(reverse("core:contact"), {
        "name": "Contact", "email": "contact@example.test", "message": "Please help with a domain", "subject": "Domain",
    }, REMOTE_ADDR="192.0.2.20", HTTP_X_FORWARDED_FOR="198.51.100.5")
    assert response.status_code == 200
    assert ContactSubmission.objects.get().ip_address == "192.0.2.20"


@pytest.mark.django_db
@override_settings(TRUSTED_PROXY_CIDRS=[])
@pytest.mark.parametrize("stored,peer", [
    ("::ffff:192.0.2.20", "192.0.2.20"),
    ("2001:db8::20", "2001:0db8:0:0::20"),
])
def test_allowlist_and_socket_addresses_use_the_same_normalization(django_user_model, stored, peer):
    user = django_user_model.objects.create_user(email="normalized@example.test", is_staff=True)
    IPAllowlistEntry.objects.create(ip_address=stored, is_active=True)
    request = request_for(peer)
    request.user = user
    assert IPAllowlistMiddleware(lambda request: HttpResponse("allowed"))(request).status_code == 200


@pytest.mark.django_db
@override_settings(TRUSTED_PROXY_CIDRS=["10.0.0.8"])
def test_invalid_address_does_not_break_audit_persistence(django_user_model):
    user = django_user_model.objects.create_user(email="invalid-ip@example.test")
    request = RequestFactory().post("/invalid-address/", REMOTE_ADDR="10.0.0.8", HTTP_X_FORWARDED_FOR="garbage")
    request.user = user
    response = AuditLogMiddleware(lambda request: HttpResponse("ok"))(request)
    assert response.status_code == 200
    assert AuditLog.objects.get(action="POST /invalid-address/").ip_address is None
