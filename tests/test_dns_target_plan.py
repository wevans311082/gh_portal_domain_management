from copy import deepcopy
import json
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from apps.domains.contact_bindings import _fingerprint
from apps.domains.dns_target_plan import build_target_plan
from tests.test_dns_record_plan import cf, plan
from tests.test_whm_dns_inventory import rr as whm_rr


def rr(*args, **kwargs):
    return {**whm_rr(*args, **kwargs), "ttl": 3600}


def receipt(rows, provider="cpanel", exists=True):
    return {"provider": provider, "name": "example.com.", "scope": "a" * 64, "exists": exists,
            "zone": {"name": "example.com"} if exists else None, "records": [json.dumps(row) for row in rows]}


def compare(source, target):
    return build_target_plan(source_plan=source, target_receipt=target, target_digest=_fingerprint(target))


def test_exact_whm_answer_is_kept_and_missing_rrset_created_offline():
    source = plan([cf("A", "192.0.2.1"), cf("MX", "mail.example.test", 2, priority=10)])
    target = receipt([rr(1, "A", ["192.0.2.1"], "example.com.")])
    with patch("requests.request", side_effect=AssertionError("No network")):
        result = compare(source, target)
    assert not result["blocked"]
    assert [action["action"] for action in result["actions"]] == ["keep", "create"]


def test_known_absence_creates_records_without_fabricating_target_zone():
    result = compare(plan([cf("A", "192.0.2.1")]), receipt([], exists=False))
    assert not result["blocked"] and result["target_exists"] is False
    assert result["actions"][0]["action"] == "create"


@pytest.mark.parametrize("change", ["missing-exists", "wrong-zone", "wrong-provider", "missing-records", "absent-with-records"])
def test_incomplete_or_wrong_target_evidence_never_means_absence(change):
    target = receipt([])
    if change == "missing-exists":
        del target["exists"]
    elif change == "wrong-zone":
        target["name"] = "other.example."
    elif change == "wrong-provider":
        target["provider"] = "cloudflare"
    elif change == "missing-records":
        del target["records"]
    else:
        target.update(exists=False, zone=None, records=["{}"])
    with pytest.raises(ValidationError):
        compare(plan([cf("A", "192.0.2.1")]), target)


@pytest.mark.parametrize("kind,values,reason", [
    ("A", ["192.0.2.2"], "rrset_answers_differ"),
    ("CNAME", ["other.example."], "cname_conflict"),
])
def test_target_conflicts_never_become_overwrite_or_delete_actions(kind, values, reason):
    result = compare(plan([cf("A", "192.0.2.1", name="www.example.com")]), receipt([rr(1, kind, values, "www.example.com.")]))
    assert result["blocked"] and any(action["reason"] == reason for action in result["actions"])
    assert not any(action["action"] in {"update", "delete"} for action in result["actions"])


def test_target_ttl_difference_requires_review():
    target = rr(1, "A", ["192.0.2.1"], "example.com.")
    target["ttl"] = 7200
    result = compare(plan([cf("A", "192.0.2.1")]), receipt([target]))
    assert result["blocked"] and result["actions"][0]["reason"] == "ttl_differs"


def test_target_only_answers_require_explicit_retention_acknowledgement():
    result = compare(plan([cf("A", "192.0.2.1")]), receipt([rr(1, "TXT", ["target-only"], "other.example.com.")]))
    assert not result["blocked"] and "retain_target_only_records" in result["required_acknowledgements"]
    assert any(action["action"] == "retain" for action in result["actions"])


def test_unknown_target_record_is_retained_and_blocks_plan():
    result = compare(plan([cf("A", "192.0.2.1")]), receipt([rr(1, "HTTPS", ["1 ."], "example.com.")]))
    assert result["blocked"] and len(result["target_plan"]["entries"]) == 1
    assert result["target_plan"]["entries"][0]["reason"] == "unsupported_record_type"


def test_cloudflare_proxy_conflict_requires_review_and_metadata_is_not_overwritten():
    source = plan([rr(1, "A", ["192.0.2.1"], "example.com.")], source="cpanel", target="cloudflare")
    target = receipt([cf("A", "192.0.2.1", proxied=True)], provider="cloudflare")
    assert compare(source, target)["actions"][0]["reason"] == "provider_behaviour_differs"
    target = receipt([cf("A", "192.0.2.1", comment="Retain", tags=["owner:customer"])], provider="cloudflare")
    result = compare(source, target)
    assert not result["blocked"] and result["required_acknowledgements"] == ["retain_target_metadata"]


def test_full_rrset_comparison_does_not_merge_extra_target_answers():
    target = receipt([rr(1, "A", ["192.0.2.1"], "example.com."), rr(2, "A", ["192.0.2.2"], "example.com.")])
    result = compare(plan([cf("A", "192.0.2.1")]), target)
    assert result["blocked"] and result["actions"][0]["reason"] == "rrset_answers_differ"


def test_source_and_target_digests_bind_comparison():
    source, target = plan([cf("A", "192.0.2.1")]), receipt([])
    changed = deepcopy(source)
    changed["entries"] = []
    with pytest.raises(ValidationError):
        compare(changed, target)
    with pytest.raises(ValidationError):
        build_target_plan(source_plan=source, target_receipt=target, target_digest="changed")
    assert compare(source, target) == compare(source, target)


def test_duplicate_target_answers_with_different_proxy_options_are_not_silently_collapsed():
    source = plan([rr(1, "A", ["192.0.2.1"], "example.com.")], source="cpanel", target="cloudflare")
    target = receipt([cf("A", "192.0.2.1", 1, proxied=True), cf("A", "192.0.2.1", 2, proxied=False)], provider="cloudflare")
    result = compare(source, target)
    assert result["blocked"]
    assert all(entry["reason"] == "duplicate_provider_options_conflict" for entry in result["target_plan"]["entries"])
