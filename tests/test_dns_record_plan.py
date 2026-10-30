import base64
import json
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from apps.domains.contact_bindings import _fingerprint
from apps.domains.dns_record_plan import build_record_plan
from tests.test_whm_dns_inventory import payload, rr


def plan(rows, source="cloudflare", target="cpanel"):
    receipt = {"zone": {"name": "example.com"}, "records": [json.dumps(row, sort_keys=True) for row in rows]}
    return build_record_plan(source_provider=source, target_provider=target, zone_name="example.com",
        inventory_receipt=receipt, inventory_digest=_fingerprint(receipt))


def cf(kind, content, number=1, **fields):
    return {"id": f"{number:032x}", "name": "example.com", "type": kind, "content": content, "ttl": 3600, "proxied": False, **fields}


@pytest.mark.parametrize("kind,parts,record", [
    ("A", ["192.0.2.1"], cf("A", "192.0.2.1")),
    ("AAAA", ["2001:db8::1"], cf("AAAA", "2001:db8::1")),
    ("MX", ["10", "mail.example.test."], cf("MX", "mail.example.test", priority=10)),
    ("MX", ["0", "."], cf("MX", ".", priority=0)),
    ("TXT", [b"first\xff", b"second"], cf("TXT", '"first\\255" "second"')),
    ("SRV", ["0", "5", "5060", "sip.example.test."], cf("SRV", "0 5 5060 sip.example.test.", name="_sip._tcp.example.com",
        data={"priority": 0, "weight": 5, "port": 5060, "target": "sip.example.test"})),
    ("CAA", ["0", "issue", b'ca.example; account="a"'], cf("CAA", '0 issue "ca.example; account=\\034a\\034"',
        data={"flags": 0, "tag": "issue", "value": 'ca.example; account="a"'})),
])
def test_cloudflare_and_whm_records_produce_identical_dns_wire_data(kind, parts, record):
    name = "_sip._tcp.example.com." if kind == "SRV" else "example.com."
    first = plan([record])["entries"][0]
    second = plan([rr(1, kind, parts, name)], source="cpanel", target="cloudflare")["entries"][0]
    assert first["disposition"] == second["disposition"] == "copy"
    assert first["record"]["rdata_wire"] == second["record"]["rdata_wire"]


def test_txt_chunks_and_binary_octets_are_not_concatenated_or_lossily_decoded():
    result = plan([rr(1, "TXT", [b"first\xff", b"second"])], source="cpanel", target="cloudflare")
    record = result["entries"][0]["record"]
    assert base64.b64decode(record["rdata_wire"]) == b"\x06first\xff\x06second"
    assert record["rdata_text"] == '"first\\255" "second"'


def test_every_zone_line_remains_visible_in_the_plan():
    rows = payload()["data"]["payload"]
    result = plan(rows, source="cpanel", target="cloudflare")
    assert len(result["entries"]) == len(rows)
    assert [item["disposition"] for item in result["entries"][:5]] == ["metadata", "metadata", "provider_managed", "provider_managed", "provider_managed"]
    assert result["entries"][-1]["reason"] == "unsupported_record_type" and result["blocked"]
    assert all(item["source"] for item in result["entries"])


def test_proxy_removal_is_an_explicit_acknowledgement():
    result = plan([cf("A", "192.0.2.1", proxied=True, comment="Keep this", tags=["owner:customer"])])
    assert result["required_acknowledgements"] == ["cloudflare_proxy_removed", "provider_metadata_retained_in_history"]


@pytest.mark.parametrize("record,reason", [
    (cf("A", "192.0.2.1", ttl=1), "explicit_target_ttl_required"),
    (cf("CNAME", "target.example.test"), "apex_cname_requires_flattening"),
    (cf("DS", "12345 13 2 ABCD"), "dnssec_requires_separate_plan"),
    (cf("TXT", "unquoted multiple words"), "invalid_record"),
    (cf("A", "not-an-address"), "invalid_record"),
])
def test_unrepresentable_or_ambiguous_records_block_approval(record, reason):
    result = plan([record])
    assert result["blocked"] and result["entries"][0]["reason"] == reason


def test_duplicate_dns_answers_are_explicitly_linked_without_dropping_source_rows():
    result = plan([cf("A", "192.0.2.1", 1), cf("A", "192.0.2.1", 2)])
    assert len(result["entries"]) == 2 and result["entries"][1]["disposition"] == "duplicate"
    assert result["entries"][1]["duplicate_of"] == "cloudflare:" + f"{1:032x}"


@pytest.mark.parametrize("conflict", ["ttl", "cname", "multiple-cname"])
def test_dns_record_set_conflicts_block_the_whole_plan(conflict):
    if conflict == "ttl":
        rows = [cf("A", "192.0.2.1", 1), cf("A", "192.0.2.2", 2, ttl=7200)]
    else:
        rows = [cf("CNAME", "first.example.test", 1, name="www.example.com"),
                cf("A" if conflict == "cname" else "CNAME", "192.0.2.1" if conflict == "cname" else "second.example.test", 2, name="www.example.com")]
    result = plan(rows)
    assert result["blocked"] and all(item["disposition"] == "blocked" for item in result["entries"])


def test_plan_is_deterministic_offline_and_rejects_changed_receipt_digest():
    with patch("requests.request", side_effect=AssertionError("No provider calls")):
        assert plan([cf("MX", "mail.example.test", priority=10)]) == plan([cf("MX", "mail.example.test", priority=10)])
    with pytest.raises(ValidationError):
        build_record_plan(source_provider="cloudflare", target_provider="cpanel", zone_name="example.com",
            inventory_receipt={"changed": True}, inventory_digest="wrong")


def test_private_routing_cannot_be_copied_to_public_dns_without_a_separate_plan():
    result = plan([cf("A", "192.0.2.1", private_routing=True)])
    assert result["blocked"] and result["entries"][0]["reason"] == "private_routing_requires_separate_plan"


def test_unknown_provider_record_settings_require_review():
    result = plan([cf("A", "192.0.2.1", settings={"unknown_feature": True})])
    assert result["blocked"] and result["entries"][0]["reason"] == "provider_record_settings_require_review"


def test_cloudflare_record_options_are_preserved_or_explicitly_acknowledged():
    record = cf("A", "192.0.2.1", proxied=True, settings={"ipv4_only": True}, comment="Keep", tags=["owner:customer"])
    retained = plan([record], target="cloudflare")
    assert retained["entries"][0]["record"]["provider_options"] == {"proxied": True, "settings": {"ipv4_only": True}, "comment": "Keep", "tags": ["owner:customer"]}
    removed = plan([record])
    assert "cloudflare_record_settings_removed" in removed["required_acknowledgements"]


def test_explicit_ttl_override_resolves_automatic_ttl_without_rewriting_source():
    source = cf("A", "192.0.2.1", ttl=1)
    receipt = {"zone": {"name": "example.com"}, "records": [json.dumps(source)]}
    result = build_record_plan(source_provider="cloudflare", target_provider="cpanel", zone_name="example.com",
        inventory_receipt=receipt, inventory_digest=_fingerprint(receipt), ttl_overrides={"cloudflare:" + source["id"]: 300})
    assert not result["blocked"] and result["entries"][0]["record"]["ttl"] == 300
    assert json.loads(result["entries"][0]["source"])["ttl"] == 1
