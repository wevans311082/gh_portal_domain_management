import json

import pytest
from django.core.exceptions import ValidationError

from apps.domains.contact_bindings import _fingerprint
from apps.domains.dns_payloads import compile_record_payload
from apps.domains.dns_apply_plan import build_apply_plan
from apps.domains.dns_record_plan import build_record_plan
from tests.test_dns_record_plan import plan, cf
from tests.test_dns_target_plan import rr, receipt


def compile(source, target):
    target["zone"]["serial"] = 123
    body = build_apply_plan(source_plan=source, target_receipt=target, target_digest=_fingerprint(target))
    return compile_record_payload(body)


@pytest.mark.parametrize("kind,values,record", [
    ("A", ["192.0.2.1"], cf("A", "192.0.2.1")),
    ("AAAA", ["2001:db8::1"], cf("AAAA", "2001:db8::1")),
    ("MX", ["10", "mail.example.test."], cf("MX", "mail.example.test", priority=10)),
    ("MX", ["0", "."], cf("MX", ".", priority=0)),
    ("TXT", ["chunk1", "chunk2"], cf("TXT", '"chunk1" "chunk2"')),
    ("SRV", ["0", "5", "5060", "sip.example.test."], cf("SRV", "0 5 5060 sip.example.test.", name="_sip._tcp.example.com",
        data=dict(priority=0, weight=5, port=5060, target="sip.example.test"))),
    ("CAA", ["0", "issue", 'ca.example; account="a"'], cf("CAA", '0 issue "ca.example; account=\\034a\\034"',
        data=dict(flags=0, tag="issue", value='ca.example; account="a"'))),
    ("NS", ["ns.delegated.example."], cf("NS", "ns.delegated.example", name="child.example.com")),
])
def test_supported_answers_round_trip_through_both_provider_payload_encodings(kind, values, record):
    name = record["name"] + "."
    whm = compile(plan([record]), receipt([]))
    assert whm["serial"] == 123 and whm["remove"] == []
    encoded = json.loads(whm["add"][0])
    assert encoded["data"] == values and encoded["dname"] == name
    source = plan([rr(1, kind, values, name)], source="cpanel", target="cloudflare")
    cloudflare = compile(source, receipt([], provider="cloudflare"))
    row = {"id": "a" * 32, **cloudflare["posts"][0]}
    inventory = {"zone": {"name": "example.com"}, "records": [json.dumps(row)]}
    decoded = build_record_plan(source_provider="cloudflare", target_provider="cloudflare", zone_name="example.com",
        inventory_receipt=inventory, inventory_digest=_fingerprint(inventory))
    assert not decoded["blocked"] and decoded["entries"][0]["record"] == source["entries"][0]["record"]


def test_binary_txt_is_retained_but_cannot_be_lossily_encoded_as_whm_json_strings():
    source = plan([cf("TXT", '"binary\\255"')])
    with pytest.raises(ValidationError, match="binary"):
        compile(source, receipt([]))


def test_unicode_txt_keeps_original_utf8_bytes_and_chunk_boundaries():
    result = compile(plan([cf("TXT", '"caf\\195\\169" "second"')]), receipt([]))
    assert json.loads(result["add"][0])["data"] == ["café", "second"]


def test_replacement_deletes_every_original_identity_and_adds_each_source_answer_once():
    source = plan([cf("A", "192.0.2.1"), cf("A", "192.0.2.1", 2)])
    target = receipt([rr(1, "A", ["192.0.2.2"], "example.com."), rr(2, "A", ["192.0.2.3"], "example.com.")])
    target["zone"]["serial"] = 123
    body = build_apply_plan(source_plan=source, target_receipt=target, target_digest=_fingerprint(target),
        decisions=[dict(name="example.com.", type="A", action="replace")])
    result = compile_record_payload(body)
    assert result["remove"] == [1, 2] and len(result["add"]) == 1


def test_blocked_or_tampered_plan_cannot_generate_a_provider_request():
    target = receipt([])
    body = build_apply_plan(source_plan=plan([cf("A", "192.0.2.1")]), target_receipt=target, target_digest=_fingerprint(target))
    body["actions"] = []
    with pytest.raises(ValidationError):
        compile_record_payload(body)


@pytest.mark.parametrize("provider,count", [("cpanel", 1251), ("cloudflare", 1751)])
def test_oversized_whole_rrset_replacements_require_review_before_any_submission(provider, count):
    if provider == "cpanel":
        source = plan([cf("TXT", f'"desired-{index}"', index + 1) for index in range(count)])
        target = receipt([rr(index, "TXT", [f"existing-{index}"], "example.com.") for index in range(count)])
        target["zone"]["serial"] = 123
    else:
        source = plan([rr(index, "TXT", [f"desired-{index}"], "example.com.") for index in range(count)], source="cpanel", target="cloudflare")
        target = receipt([cf("TXT", f'"existing-{index}"', index + 1) for index in range(count)], provider="cloudflare")
    body = build_apply_plan(source_plan=source, target_receipt=target, target_digest=_fingerprint(target),
        decisions=[dict(name="example.com.", type="TXT", action="replace")])
    with pytest.raises(ValidationError, match="capacity"):
        compile_record_payload(body)
