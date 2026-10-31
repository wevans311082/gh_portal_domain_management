import json
from unittest.mock import Mock, patch

import pytest

from apps.cloudflare_integration.services import CloudflareError
from apps.provisioning.whm_client import WHMClient, WHMClientError
from tests.test_cloudflare_scope import provider
from tests.test_whm_dns_inventory import client_for


def response(wire, status=200):
    result = Mock(status_code=status, headers={})
    result.iter_content.return_value = iter([wire])
    return result


def session(value):
    transport = Mock()
    transport.__enter__ = Mock(return_value=transport)
    transport.__exit__ = Mock(return_value=False)
    transport.post.return_value = value
    return transport


def test_cloudflare_batch_is_one_isolated_tls_post_without_redirects_environment_auth_or_retries():
    value = response(b'{"success":true,"result":{"deletes":[],"posts":[]}}')
    transport = session(value)
    payload = {"deletes": [], "posts": [{"name": "example.com", "type": "A", "content": "192.0.2.1", "ttl": 3600}]}
    with patch("apps.cloudflare_integration.services.requests.Session", return_value=transport):
        result = provider().apply_dns_batch("b" * 32, payload)
    assert result["success"] is True and transport.post.call_count == 1 and transport.trust_env is False
    assert transport.post.call_args.args == ("https://api.cloudflare.com/client/v4/zones/" + "b" * 32 + "/dns_records/batch",)
    options = transport.post.call_args.kwargs
    assert options["json"] == payload and options["verify"] is True and options["allow_redirects"] is False
    assert options["timeout"] == (10, 30) and options["stream"] is True
    value.close.assert_called_once()


@pytest.mark.parametrize("wire,status", [
    (b'{"success":false,"success":true,"result":{}}', 200),
    (b'{"success":true,"result":null}', 200), (b'{"success":1,"result":{}}', 200),
    (b'{"success":true,"result":{"invalid":NaN}}', 200),
    (b'PRIVATE response', 503), (b'PRIVATE redirect', 302),
    (b'x' * (4 * 1024 * 1024 + 1), 200),
], ids=["duplicate-key", "null-result", "numeric-success", "nan", "server-error", "redirect", "oversized"])
def test_cloudflare_ambiguous_acknowledgements_hold_for_reconciliation_without_leaking_provider_body(wire, status):
    value = response(wire, status)
    transport = session(value)
    with patch("apps.cloudflare_integration.services.requests.Session", return_value=transport):
        with pytest.raises(CloudflareError) as error:
            provider().apply_dns_batch("b" * 32, {"deletes": [{"id": "a" * 32}], "posts": []})
    assert error.value.uncertain is True and "PRIVATE" not in str(error.value)
    assert transport.post.call_count == 1
    value.close.assert_called_once()


def test_whm_mass_edit_passes_serial_and_repeated_parameters_to_strict_transport():
    client = client_for()
    client._strict_dns_api = Mock(return_value={"data": {"new_serial": 124}})
    payload = {"zone": "example.com", "serial": 123, "remove": [5, 6], "add": [json.dumps({"dname": "example.com.", "ttl": 3600, "record_type": "MX", "data": ["10", "mail.example.test."]})]}
    assert WHMClient.apply_dns_batch(client, payload) == {"new_serial": 124}
    client._strict_dns_api.assert_called_once_with("mass_edit_dns_zone", payload)


@pytest.mark.parametrize("serial", [None, True, "124", 123, -1, 4294967296])
def test_whm_ambiguous_or_unchanged_acknowledged_serial_is_not_success(serial):
    client = client_for()
    client._strict_dns_api = Mock(return_value={"data": {"new_serial": serial}})
    with pytest.raises(WHMClientError):
        WHMClient.apply_dns_batch(client, {"zone": "example.com", "serial": 123, "remove": [5], "add": []})
    assert client._strict_dns_api.call_count == 1


@pytest.mark.parametrize("serial", [None, True, "123", -1, 4294967296])
def test_whm_invalid_precondition_serial_cannot_send_a_batch(serial):
    client = client_for()
    client._strict_dns_api = Mock()
    with pytest.raises(WHMClientError):
        WHMClient.apply_dns_batch(client, {"zone": "example.com", "serial": serial, "remove": [5], "add": []})
    client._strict_dns_api.assert_not_called()
