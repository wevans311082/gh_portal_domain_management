import base64
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.domains.dns_changes import backend_scope
from apps.provisioning.dns_inventory import read_inventory
from apps.provisioning.whm_client import WHMClientError, WHMClient


def b64(value):
    return base64.b64encode(value if type(value) is bytes else value.encode("ascii")).decode("ascii")


def rr(index, kind, values, name="example.com."):
    return {"type": "record", "line_index": index, "record_type": kind, "dname_b64": b64(name), "ttl": 14400,
        "data_b64": [b64(value) for value in values]}


def payload(name="example.com"):
    return {"metadata": {"result": 1, "version": 1, "command": "parse_dns_zone"}, "data": {"payload": [
        {"type": "control", "line_index": 0, "text_b64": b64("$ORIGIN " + name + ".")},
        {"type": "comment", "line_index": 1, "text_b64": b64(b"; binary comment \xff")},
        rr(2, "SOA", ["ns1.example.test.", "hostmaster.example.test.", "2026103001", "3600", "900", "1209600", "300"], name + "."),
        rr(3, "NS", ["ns1.example.test."], name + "."), rr(4, "NS", ["ns2.example.test."], name + "."),
        rr(5, "MX", ["10", "mail.example.test."], name + "."), rr(6, "TXT", [b"first\xff", b"second"], name + "."),
        rr(7, "HTTPS", ["1", ".", "alpn=h2"], name + "."),
    ]}}


def client_for(values=None):
    client = SimpleNamespace(host="whm.example.test", port=2087, username="root", api_token="PRIVATE-TOKEN",
        base_url="https://whm.example.test:2087/json-api", dns_nameservers=("ns1.example.test", "ns2.example.test"),
        session=SimpleNamespace(verify=True, headers={"Authorization": "whm root:PRIVATE-TOKEN"}))
    data = payload() if values is None else values
    client.parse_dns_zone = Mock(side_effect=lambda name: deepcopy(data))
    return client


def read(client):
    return read_inventory(domain_name="example.com", client=client, expected_scope=backend_scope("cpanel", client))


def test_whm_inventory_preserves_every_line_binary_chunk_and_mail_field():
    client = client_for()
    result = read(client)
    retained = [json.loads(value) for value in result.records]
    assert len(retained) == 8 and result.zone.serial == 2026103001
    assert base64.b64decode(retained[6]["data_b64"][0]) == b"first\xff"
    assert retained[5]["data_b64"] == [b64("10"), b64("mail.example.test.")]
    assert result.unsupported_types == ("BINARY_DATA", "HTTPS")
    assert client.parse_dns_zone.call_count == 2


@pytest.mark.parametrize("change", ["bad-base64", "duplicate-index", "missing-soa", "wrong-owner", "unknown-control", "unknown-status"])
def test_whm_incomplete_or_ambiguous_payload_requires_review(change):
    data = payload()
    rows = data["data"]["payload"]
    if change == "bad-base64":
        rows[-1]["data_b64"] = ["not-base64"]
    elif change == "duplicate-index":
        rows[-1]["line_index"] = 2
    elif change == "missing-soa":
        rows.pop(2)
    elif change == "wrong-owner":
        rows[-1]["dname_b64"] = b64("unrelated.test.")
    elif change == "unknown-control":
        rows[0]["text_b64"] = b64("$INCLUDE external-zone")
    else:
        data["metadata"]["result"] = "1"
    with pytest.raises(WHMClientError):
        read(client_for(data))


def test_changed_zone_or_serial_between_reads_is_not_stable_inventory():
    client = client_for()
    first, second = payload(), payload()
    second["data"]["payload"][5]["data_b64"] = [b64("20"), b64("different.example.test.")]
    client.parse_dns_zone.side_effect = [first, second]
    with pytest.raises(WHMClientError, match="changed during"):
        read(client)


def test_nameserver_configuration_is_part_of_whm_request_authority():
    client = client_for()
    scope = backend_scope("cpanel", client)
    client.dns_nameservers = ("different1.example.test", "different2.example.test")
    with pytest.raises(WHMClientError, match="configuration changed"):
        read_inventory(domain_name="example.com", client=client, expected_scope=scope)
    client.parse_dns_zone.assert_not_called()


def test_zone_nameservers_must_match_reviewed_platform_configuration():
    data = payload()
    data["data"]["payload"][3]["data_b64"] = [b64("other.example.test.")]
    with pytest.raises(WHMClientError, match="authority"):
        read(client_for(data))


def test_whm_parse_transport_uses_bounded_read_without_redirects_or_legacy_fallback():
    client = WHMClient.__new__(WHMClient)
    client.base_url = "https://whm.example.test:2087/json-api"
    client.session = SimpleNamespace(get=Mock(return_value=SimpleNamespace(status_code=200, json=lambda: payload())))
    assert client.parse_dns_zone("example.com")["metadata"]["result"] == 1
    assert client.session.get.call_args.kwargs == {"params": {"api.version": 1, "zone": "example.com"}, "timeout": (10, 30), "allow_redirects": False}


@pytest.mark.parametrize("status", [302, 403, 500])
def test_whm_inventory_transport_rejects_http_failure_without_raw_error(status):
    client = WHMClient.__new__(WHMClient)
    client.base_url = "https://whm.example.test:2087/json-api"
    client.session = SimpleNamespace(get=Mock(return_value=SimpleNamespace(status_code=status, json=lambda: {"PRIVATE": "TOKEN"})))
    with pytest.raises(WHMClientError) as error:
        client.parse_dns_zone("example.com")
    assert "PRIVATE" not in str(error.value) and "TOKEN" not in str(error.value)


def test_relative_owner_and_structured_srv_parts_are_retained():
    data = payload()
    record = rr(8, "SRV", ["0", "5", "5060", "sip.example.test."], "_sip._tcp")
    data["data"]["payload"].append(record)
    result = read(client_for(data))
    assert json.loads(result.records[-1]) == record


@pytest.mark.parametrize("owner,accepted", [(b"\xff.example.com.", True), (b"\xff.unrelated.test.", False)])
def test_binary_owners_are_preserved_only_with_matching_zone_boundary(owner, accepted):
    data = payload()
    data["data"]["payload"][-1]["dname_b64"] = b64(owner)
    if accepted:
        assert "BINARY_DATA" in read(client_for(data)).unsupported_types
    else:
        with pytest.raises(WHMClientError, match="unrelated binary"):
            read(client_for(data))


def test_thirteen_platform_nameservers_fit_the_portable_setting_schema():
    from apps.admin_tools.setting_schema import validate_setting, SettingValidationError
    hosts = ",".join(f"ns{i}." + ("a" * 60 + ".") * 3 + "example.test" for i in range(1, 14))
    assert validate_setting("WHM_NAMESERVERS", hosts) == (hosts, False)
    with pytest.raises(SettingValidationError):
        validate_setting("WHM_NAMESERVERS", hosts + ",ns14.example.test")
