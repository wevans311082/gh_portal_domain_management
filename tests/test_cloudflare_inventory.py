import json
from unittest.mock import Mock

import pytest

from apps.cloudflare_integration.inventory import read_inventory
from apps.cloudflare_integration.scope import provider_scope
from apps.cloudflare_integration.services import CloudflareError
from tests.test_cloudflare_scope import provider
from tests.test_cloudflare_zones import zone


def row(number=1, **changes):
    return {"id": f"{number:032x}", "type": "MX", "name": "example.com", "ttl": 3600,
        "content": "mail.example.test", "priority": 10, "proxied": False, **changes}


def page(rows, number=1, pages=1, total=None, size=200):
    return {"success": True, "result": rows, "result_info": {"page": number, "count": len(rows),
        "total_pages": pages, "per_page": size, "total_count": len(rows) if total is None else total}}


def client_for(pages):
    client = provider()
    client.get_zone = Mock(return_value={"success": True, "result": zone()})
    client.list_dns_records = Mock(side_effect=pages)
    client.create_dns_record = Mock()
    return client


def read(client):
    return read_inventory(zone_id="b" * 32, domain_name="example.com", client=client, expected_scope=provider_scope(client))


def test_paginated_inventory_preserves_mail_structured_data_and_unfamiliar_records():
    srv = row(2, type="SRV", name="_sip._tcp.example.com", content="0 5 5060 sip.example.test",
        data={"priority": 0, "weight": 5, "port": 5060, "target": "sip.example.test"})
    unfamiliar = row(3, type="HTTPS", content="1 . alpn=h2", comment="Retain this", tags=["owner:customer"])
    client = client_for([page([row(), srv], pages=2, total=3, size=2), page([unfamiliar], number=2, pages=2, total=3, size=2)])
    result = read(client)
    records = [json.loads(value) for value in result.records]
    assert records[0]["priority"] == 10 and records[1]["data"]["port"] == 5060
    assert records[2]["comment"] == "Retain this" and result.unsupported_types == ("HTTPS",)
    assert client.list_dns_records.call_count == 2 and client.get_zone.call_count == 2
    client.create_dns_record.assert_not_called()


def test_inventory_digest_does_not_depend_on_provider_order():
    first = read(client_for([page([row(1), row(2)])]))
    second = read(client_for([page([row(2), row(1)])]))
    assert first.digest == second.digest and first.records == second.records


@pytest.mark.parametrize("changes", [{"name": "other.com"}, {"ttl": True}, {"priority": None},
    {"zone_id": "c" * 32}, {"proxied": "false"}, {"content": None}, {"id": "bad/id"}])
def test_malformed_or_wrong_zone_record_cannot_be_silently_dropped(changes):
    with pytest.raises(CloudflareError):
        read(client_for([page([row(**changes)])]))


def test_duplicate_across_pages_is_not_complete_inventory():
    with pytest.raises(CloudflareError, match="repeated"):
        read(client_for([page([row()], pages=2, total=2, size=1), page([row()], number=2, pages=2, total=2, size=1)]))


def test_missing_pagination_does_not_mean_empty_zone():
    with pytest.raises(CloudflareError, match="pagination"):
        read(client_for([{"success": True, "result": []}]))


def test_zone_changed_during_inventory_discards_all_records():
    client = client_for([page([row()])])
    client.get_zone.side_effect = [{"success": True, "result": zone()}, {"success": True, "result": zone(status="active")}]
    with pytest.raises(CloudflareError, match="zone changed"):
        read(client)


@pytest.mark.parametrize("changes", [{"total_count": 2}, {"total_count": True}, {"total_pages": 2}, {"page": 2}])
def test_inconsistent_record_counts_never_establish_complete_inventory(changes):
    payload = page([row()])
    payload["result_info"].update(changes)
    with pytest.raises(CloudflareError, match="pagination"):
        read(client_for([payload]))
