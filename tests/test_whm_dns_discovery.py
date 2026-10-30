import json
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError

from apps.admin_tools.setting_schema import validate_setting, SettingValidationError
from apps.domains.dns_changes import backend_scope, zone_creation_options
from apps.provisioning.dns_zones import zone_exists
from apps.provisioning.whm_client import WHMClient, WHMClientError
from tests.test_whm_dns_inventory import client_for


def listed(domains):
    return {"metadata": {"result": 1, "version": 1, "command": "listzones"},
            "data": {"zone": [{"domain": name, "zonefile": "not-retained"} for name in domains]}}


def test_complete_discovery_checks_twice_and_tolerates_unrelated_zone_changes():
    client = client_for()
    client.list_dns_zones = Mock(side_effect=[listed(["other.test"]), listed(["another.test"])])
    assert zone_exists(domain_name="example.com", client=client, expected_scope=backend_scope("cpanel", client)) is False
    assert client.list_dns_zones.call_count == 2
    client.list_dns_zones.side_effect = [listed(["EXAMPLE.COM."]), listed(["example.com"])]
    assert zone_exists(domain_name="example.com", client=client, expected_scope=backend_scope("cpanel", client)) is True


@pytest.mark.parametrize("payload", [None, {}, {"metadata": {"result": 0}},
    {"metadata": {"result": True, "version": 1, "command": "listzones"}, "data": {"zone": []}},
    {"metadata": {"result": 1, "version": 1, "command": "listzones"}, "data": {}},
    listed(["example.com", "EXAMPLE.COM."]), listed(["bad\n.test"]), listed(["faß.de"]), listed(["https://example.com"])])
def test_partial_malformed_or_ambiguous_lists_cannot_establish_absence(payload):
    client = client_for()
    client.list_dns_zones = Mock(return_value=payload)
    with pytest.raises(WHMClientError):
        zone_exists(domain_name="example.com", client=client, expected_scope=backend_scope("cpanel", client))


def test_nonroot_absence_and_changing_target_require_review():
    client = client_for()
    client.username = "reseller"
    client.session.headers["Authorization"] = "whm reseller:PRIVATE-TOKEN"
    client.list_dns_zones = Mock(return_value=listed([]))
    with pytest.raises(WHMClientError, match="root-account"):
        zone_exists(domain_name="example.com", client=client, expected_scope=backend_scope("cpanel", client))
    client.list_dns_zones.side_effect = [listed([]), listed(["example.com"])]
    with pytest.raises(WHMClientError, match="appeared"):
        zone_exists(domain_name="example.com", client=client, expected_scope=backend_scope("cpanel", client))


def test_scope_rotation_discards_discovery():
    client = client_for()
    scope = backend_scope("cpanel", client)
    def changed():
        client.api_token = "ROTATED"
        return listed([])
    client.list_dns_zones = Mock(side_effect=changed)
    with pytest.raises(ValidationError):
        zone_exists(domain_name="example.com", client=client, expected_scope=scope)


def response(payload, status=200):
    value = Mock(status_code=status, headers={})
    value.iter_content.return_value = iter([json.dumps(payload).encode()])
    return value


@pytest.mark.parametrize("function", ["listzones", "adddns"])
def test_strict_transport_uses_documented_get_once_without_environment_auth_or_redirects(function):
    client = client_for()
    client._strict_dns_api = lambda name, params: WHMClient._strict_dns_api(client, name, params)
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.return_value = response({"metadata": {"result": 1, "version": 1, "command": function}, "data": {"zone": []}})
    with patch("apps.provisioning.whm_client.requests.Session", return_value=session):
        if function == "listzones":
            WHMClient.list_dns_zones(client)
        else:
            assert WHMClient.create_dns_zone(client, "example.com", ip="192.0.2.44") is True
    assert session.get.call_count == 1 and session.trust_env is False
    args = session.get.call_args.kwargs
    assert args["allow_redirects"] is False and args["verify"] is True and args["stream"] is True and args["timeout"] == (10, 30)
    assert args["headers"]["Authorization"] == "whm root:PRIVATE-TOKEN"
    if function == "adddns":
        assert args["params"] == {"api.version": 1, "domain": "example.com", "ip": "192.0.2.44", "template": "simple"}


@pytest.mark.parametrize("ip", ["", "not-an-ip", "2001:db8::1", True])
def test_creation_ipv4_is_explicit_and_portable_schema_rejects_invalid_values(ip):
    client = client_for()
    client._strict_dns_api = Mock()
    with pytest.raises(WHMClientError):
        WHMClient.create_dns_zone(client, "example.com", ip=ip)
    client._strict_dns_api.assert_not_called()
    client.dns_zone_ip = ip
    if ip != "":
        with pytest.raises((ValidationError, SettingValidationError)):
            zone_creation_options("cpanel", client)
    else:
        assert zone_creation_options("cpanel", client) == {}


def test_creation_choices_capture_only_documented_template_and_owner_defaults():
    client = client_for()
    client.dns_zone_ip = "192.0.2.44"
    assert zone_creation_options("cpanel", client) == {"ip": "192.0.2.44", "template": "simple", "owner": "authenticated_user",
        "nameservers": ["ns1.example.test", "ns2.example.test"]}
    assert zone_creation_options("cloudflare", client) == {}
    assert validate_setting("WHM_DNS_ZONE_IP", "192.0.2.44") == ("192.0.2.44", False)
    with pytest.raises(SettingValidationError):
        validate_setting("WHM_DNS_ZONE_IP", "2001:db8::1")


@pytest.mark.parametrize("wire,status", [(b'{"metadata":{"result":0,"result":1}}', 200),
    (b'{"metadata":{"result":true,"version":1,"command":"listzones"}}', 200),
    (b'{}', 302), (b'x' * (4 * 1024 * 1024 + 1), 200)], ids=["duplicate-json", "boolean-result", "redirect", "oversized"])
def test_strict_transport_never_guesses_from_ambiguous_or_oversized_responses(wire, status):
    client = client_for()
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    value = Mock(status_code=status, headers={})
    value.iter_content.return_value = iter([wire])
    session.get.return_value = value
    with patch("apps.provisioning.whm_client.requests.Session", return_value=session):
        with pytest.raises(WHMClientError):
            WHMClient._strict_dns_api(client, "listzones", {})
    assert session.get.call_count == 1
    value.close.assert_called_once()
