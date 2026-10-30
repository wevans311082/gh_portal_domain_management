from copy import deepcopy
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from apps.domains.contact_bindings import _fingerprint
from apps.domains.dns_apply_plan import build_apply_plan
from tests.test_dns_record_plan import cf, plan
from tests.test_dns_target_plan import rr, receipt


def build(source=None, target=None, decisions=None):
    source = plan([cf("A", "192.0.2.1")]) if source is None else source
    target = receipt([rr(1, "A", ["192.0.2.2"], "example.com.")]) if target is None else target
    return build_apply_plan(source_plan=source, target_receipt=target, target_digest=_fingerprint(target), decisions=decisions)


def decision(kind="A", action="replace", name="example.com."):
    return dict(name=name, type=kind, action=action)


def test_differing_target_requires_explicit_whole_rrset_replacement():
    assert build()["blocked"]
    with patch("requests.request", side_effect=AssertionError("offline")):
        resolved = build(decisions=[decision()])
    assert not resolved["blocked"] and resolved["delegation_authorized"] is False
    action = resolved["actions"][0]
    assert action["action"] == "replace" and action["before"][0]["record"]["rdata_text"] == "192.0.2.2"
    assert action["after"][0]["record"]["rdata_text"] == "192.0.2.1"
    assert resolved["required_acknowledgements"] == ["replace_target_records"]


def test_replacement_does_not_merge_extra_target_answers_or_drop_original_identities():
    target = receipt([rr(1, "A", ["192.0.2.2"], "example.com."), rr(2, "A", ["192.0.2.3"], "example.com.")])
    result = build(target=target, decisions=[decision()])
    assert len(result["actions"][0]["before"]) == 2
    assert [row["rdata_text"] for row in result["final_records"]] == ["192.0.2.1"]


def test_delete_target_only_rrset_requires_distinct_explicit_consent():
    target = receipt([rr(1, "MX", ["0", "example.com."], "example.com.")])
    default = build(target=target)
    assert not default["blocked"] and default["required_acknowledgements"] == ["retain_target_only_records"]
    result = build(target=target, decisions=[decision("MX", "delete")])
    assert not result["blocked"] and result["required_acknowledgements"] == ["delete_target_records"]
    assert [row["type"] for row in result["final_records"]] == ["A"]


@pytest.mark.parametrize("source_type,target_type,source_value,target_values", [
    ("A", "CNAME", "192.0.2.1", ["old.example.test."]),
    ("CNAME", "A", "new.example.test", ["192.0.2.2"]),
])
def test_cname_transition_requires_deleting_each_conflicting_target_rrset(source_type, target_type, source_value, target_values):
    source = plan([cf(source_type, source_value, name="www.example.com")])
    target = receipt([rr(1, target_type, target_values, "www.example.com.")])
    assert build(source, target)["blocked"]
    result = build(source, target, [decision(target_type, "delete", "www.example.com.")])
    assert not result["blocked"] and [r["type"] for r in result["final_records"]] == [source_type]


def test_cname_conflict_requires_deleting_all_target_only_types():
    source = plan([cf("CNAME", "new.example.test", name="www.example.com")])
    target = receipt([rr(1, "A", ["192.0.2.2"], "www.example.com."), rr(2, "TXT", ["keep?"], "www.example.com.")])
    assert build(source, target, [decision("A", "delete", "www.example.com.")])["blocked"]
    result = build(source, target, [decision("A", "delete", "www.example.com."), decision("TXT", "delete", "www.example.com.")])
    assert not result["blocked"]


@pytest.mark.parametrize("change", [
    [], {}, [True], [dict(name="example.com.", type="A", action="merge")],
    [dict(name="example.com", type="A", action="replace")],
    [dict(name="example.com.", type="A", action="delete")],
    [dict(name="example.com.", type="MX", action="replace")],
    [dict(name="example.com.", type="A", action="replace", records=[])],
    [decision(), decision()],
])
def test_invalid_or_stale_record_decisions_do_not_guess_user_intent(change):
    if change == []:
        assert build(decisions=change)["blocked"]
    else:
        with pytest.raises(ValidationError):
            build(decisions=change)


@pytest.mark.parametrize("kind,values", [("NS", ["ns1.example.test."]), ("SOA", ["ns1.example.test.", "hostmaster.example.com.", "1", "3600", "600", "86400", "300"])])
def test_provider_managed_apex_authority_cannot_be_deleted(kind, values):
    target = receipt([rr(1, kind, values, "example.com.")])
    with pytest.raises(ValidationError):
        build(target=target, decisions=[decision(kind, "delete")])
    result = build(target=target)
    assert len(result["target_plan"]["entries"]) == 1 and not result["blocked"]


def test_unknown_target_record_cannot_be_removed_to_bypass_review():
    target = receipt([rr(1, "HTTPS", ["1 ."], "example.com.")])
    assert build(target=target)["blocked"]
    with pytest.raises(ValidationError):
        build(target=target, decisions=[decision("HTTPS", "delete")])


def test_target_absence_is_not_a_record_application_plan():
    with pytest.raises(ValidationError, match="Prepare"):
        build(target=receipt([], exists=False))


def test_exact_match_is_kept_with_target_metadata_and_cannot_be_replaced_unnecessarily():
    source = plan([rr(1, "A", ["192.0.2.1"], "example.com.")], source="cpanel", target="cloudflare")
    target = receipt([cf("A", "192.0.2.1", comment="Keep", tags=["source:customer"])], provider="cloudflare")
    result = build(source, target)
    assert result["required_acknowledgements"] == ["retain_target_metadata"]
    assert result["final_records"][0]["provider_options"]["comment"] == "Keep"
    with pytest.raises(ValidationError, match="Matching"):
        build(source, target, [decision()])


def test_ttl_and_proxy_changes_are_included_in_explicit_replacement():
    source = plan([rr(1, "A", ["192.0.2.1"], "example.com.")], source="cpanel", target="cloudflare")
    target = receipt([cf("A", "192.0.2.1", ttl=7200, proxied=True)], provider="cloudflare")
    assert build(source, target)["blocked"]
    result = build(source, target, [decision()])
    assert not result["blocked"] and result["final_records"][0]["ttl"] == 3600
    assert result["final_records"][0]["provider_options"]["proxied"] is False


def test_plan_digest_binds_decisions_complete_evidence_and_before_after_and_inputs_are_unchanged():
    source, target, choices = plan([cf("A", "192.0.2.1")]), receipt([rr(1, "A", ["192.0.2.2"], "example.com.")]), [decision()]
    original = deepcopy((source, target, choices))
    first, second = build(source, target, choices), build(source, target, choices)
    assert first == second and (source, target, choices) == original
    body = dict(first)
    assert body.pop("digest") == _fingerprint(body)
