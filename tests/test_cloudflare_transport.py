from unittest.mock import Mock, patch

import pytest
import requests

from apps.cloudflare_integration.services import CloudflareService, CloudflareError


@pytest.fixture
def provider():
    with patch("apps.cloudflare_integration.services.get_runtime_setting", return_value="PRIVATE-TOKEN"):
        return CloudflareService()


def response(payload=None, status=200):
    return Mock(status_code=status, json=Mock(return_value=payload if payload is not None else {"success": True, "result": []}))


def test_scoped_zone_lookup_has_bounded_transport_and_exact_filters(provider):
    with patch("apps.cloudflare_integration.services.requests.request", return_value=response()) as http:
        provider.list_zones("EXAMPLE.COM.", account_id="a" * 32, page=2)
    args, kwargs = http.call_args
    assert args == ("GET", "https://api.cloudflare.com/client/v4/zones")
    assert kwargs["params"] == {"name": "example.com", "account.id": "a" * 32, "page": 2, "per_page": 50, "match": "all"}
    assert kwargs["timeout"] == (10, 30) and kwargs["allow_redirects"] is False


def test_scoped_zone_creation_retains_account_and_disables_discovery_by_default(provider):
    with patch("apps.cloudflare_integration.services.requests.request", return_value=response()) as http:
        provider.create_zone("EXAMPLE.COM.", account_id="b" * 32)
    assert http.call_args.kwargs["json"] == {"name": "example.com", "jump_start": False, "account": {"id": "b" * 32}}
    assert http.call_count == 1


def test_explicit_record_pagination_uses_validated_zone_and_bounded_page(provider):
    with patch("apps.cloudflare_integration.services.requests.request", return_value=response()) as http:
        provider.list_dns_records("b" * 32, page=2, per_page=200)
    assert http.call_args.args == ("GET", "https://api.cloudflare.com/client/v4/zones/" + "b" * 32 + "/dns_records")
    assert http.call_args.kwargs["params"] == {"page": 2, "per_page": 200, "order": "name"}
    with patch("apps.cloudflare_integration.services.requests.request") as http:
        with pytest.raises(CloudflareError):
            provider.list_dns_records("bad/id", page=2)
        with pytest.raises(CloudflareError):
            provider.list_dns_records("b" * 32, page=True)
    http.assert_not_called()


@pytest.mark.parametrize("mutation", [False, True])
def test_timeout_is_sanitized_and_never_retried(provider, mutation):
    with patch("apps.cloudflare_integration.services.requests.request", side_effect=requests.Timeout("PRIVATE-TOKEN PRIVATE-RESPONSE")) as http:
        with pytest.raises(CloudflareError) as failure:
            provider._request("POST" if mutation else "GET", "zones")
    assert failure.value.uncertain is mutation
    assert "PRIVATE" not in str(failure.value) and http.call_count == 1


@pytest.mark.parametrize("payload", [{"success": False, "errors": ["PRIVATE"]}, {"success": "true"}, {}, [], None])
def test_unknown_response_never_establishes_success(provider, payload):
    fake = response()
    fake.json.return_value = payload
    with patch("apps.cloudflare_integration.services.requests.request", return_value=fake):
        with pytest.raises(CloudflareError) as failure:
            provider.create_zone("example.com", account_id="a" * 32)
    assert failure.value.uncertain and "PRIVATE" not in str(failure.value)


@pytest.mark.parametrize("status,uncertain", [(302, True), (403, False), (500, True)])
def test_http_failures_do_not_redirect_or_expose_provider_body(provider, status, uncertain):
    with patch("apps.cloudflare_integration.services.requests.request", return_value=response(status=status)) as http:
        with pytest.raises(CloudflareError) as failure:
            provider.create_zone("example.com")
    assert failure.value.uncertain is uncertain and http.call_count == 1


@pytest.mark.parametrize("name,account,page", [("bad host", "a" * 32, 1), ("example.com", "bad/account", 1), ("example.com", "a" * 32, True)])
def test_invalid_lookup_is_rejected_before_transport(provider, name, account, page):
    with patch("apps.cloudflare_integration.services.requests.request") as http:
        with pytest.raises(CloudflareError):
            provider.list_zones(name, account_id=account, page=page)
    http.assert_not_called()
