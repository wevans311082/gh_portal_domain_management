from unittest.mock import Mock

import pytest

from apps.cloudflare_integration.scope import provider_scope
from apps.cloudflare_integration.services import CloudflareError
from apps.cloudflare_integration.zones import find_zone, observe_zone
from tests.test_cloudflare_scope import provider


def zone(**changes):
    return {"id": "b" * 32, "name": "example.com", "account": {"id": "a" * 32},
        "name_servers": ["ns1.example.test", "ns2.example.test"], "type": "full", "status": "pending",
        "paused": False, "PRIVATE": "DISCARD-ME", **changes}


def page(rows, number=1, pages=1):
    return {"success": True, "result": rows, "result_info": {
        "page": number, "per_page": 50, "count": len(rows), "total_pages": pages}}


def test_lookup_and_detail_observations_retain_only_exact_scope_and_complete_delegation():
    client = provider()
    scope = provider_scope(client)
    hosts = [f"ns{i}.example.test" for i in range(1, 14)]
    client.list_zones = Mock(return_value=page([zone(name_servers=hosts)]))
    client.get_zone = Mock(return_value={"success": True, "result": zone(name_servers=hosts, status="active")})
    result = find_zone(domain_name="EXAMPLE.COM.", client=client, expected_scope=scope)
    assert result.nameservers == tuple(hosts) and result.status == "pending"
    observed = observe_zone(result.zone_id, domain_name="example.com", client=client, expected_scope=scope)
    assert observed.status == "active" and "DISCARD" not in repr(observed)


def test_absence_requires_complete_search_and_never_calls_creation():
    client = provider()
    client.list_zones = Mock(side_effect=[page([], pages=2), page([], number=2, pages=2)])
    client.create_zone = Mock()
    assert find_zone(domain_name="example.com", client=client, expected_scope=provider_scope(client)) is None
    assert client.list_zones.call_count == 2
    client.create_zone.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"name": "other.com"}, {"account": {"id": "c" * 32}}, {"id": "bad/id"},
    {"type": "partial"}, {"status": "moved"}, {"status": {}}, {"paused": None},
    {"name_servers": ["same.example.test", "same.example.test"]},
    {"name_servers": ["invalid host", "ns2.example.test"]}, {"name_servers": []},
])
def test_wrong_or_incomplete_zone_is_not_adoptable(changes):
    client = provider()
    client.list_zones = Mock(return_value=page([zone(**changes)]))
    with pytest.raises(CloudflareError):
        find_zone(domain_name="example.com", client=client, expected_scope=provider_scope(client))


@pytest.mark.parametrize("changes", [{"page": True}, {"count": 2}, {"total_pages": 6}, {"total_pages": None}])
def test_incomplete_search_cannot_establish_absence(changes):
    client = provider()
    payload = page([])
    payload["result_info"].update(changes)
    client.list_zones = Mock(return_value=payload)
    with pytest.raises(CloudflareError):
        find_zone(domain_name="example.com", client=client, expected_scope=provider_scope(client))


def test_duplicate_on_later_page_is_ambiguous():
    client = provider()
    client.list_zones = Mock(side_effect=[page([zone()], pages=2), page([zone()], number=2, pages=2)])
    with pytest.raises(CloudflareError, match="Multiple"):
        find_zone(domain_name="example.com", client=client, expected_scope=provider_scope(client))


def test_scope_changed_during_read_discards_result():
    client = provider()
    original = provider_scope(client)
    def read(*args, **kwargs):
        client.account_id = "c" * 32
        return page([])
    client.list_zones = Mock(side_effect=read)
    with pytest.raises(CloudflareError, match="configuration changed"):
        find_zone(domain_name="example.com", client=client, expected_scope=original)


def test_detail_result_must_match_requested_identifier():
    client = provider()
    client.get_zone = Mock(return_value={"success": True, "result": zone(id="c" * 32)})
    with pytest.raises(CloudflareError, match="identifier changed"):
        observe_zone("b" * 32, domain_name="example.com", client=client, expected_scope=provider_scope(client))


@pytest.mark.parametrize("field,value", [("total_pages", 3), ("per_page", 5)])
def test_pagination_drift_cannot_establish_absence(field, value):
    client = provider()
    second = page([], number=2, pages=2)
    second["result_info"][field] = value
    client.list_zones = Mock(side_effect=[page([], pages=2), second])
    with pytest.raises(CloudflareError, match="inconsistent"):
        find_zone(domain_name="example.com", client=client, expected_scope=provider_scope(client))
