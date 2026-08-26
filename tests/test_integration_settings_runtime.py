import pytest

from apps.admin_tools.models import IntegrationSetting
from apps.core.runtime_settings import get_runtime_setting
from apps.domains.resellerclub_client import ResellerClubClient


@pytest.mark.django_db
def test_runtime_setting_prefers_database_value(settings):
    settings.STRIPE_SECRET_KEY = "env-value"
    IntegrationSetting.set_value("STRIPE_SECRET_KEY", "db-value", is_secret=True)

    assert get_runtime_setting("STRIPE_SECRET_KEY", "") == "db-value"


@pytest.mark.django_db
def test_resellerclub_client_uses_database_credentials(settings):
    settings.RESELLERCLUB_RESELLER_ID = "env-reseller"
    settings.RESELLERCLUB_API_KEY = "env-key"
    settings.RESELLERCLUB_API_URL = "https://test.httpapi.com/api"

    IntegrationSetting.set_value("RESELLERCLUB_RESELLER_ID", "db-reseller", is_secret=True)
    IntegrationSetting.set_value("RESELLERCLUB_API_KEY", "db-key", is_secret=True)
    IntegrationSetting.set_value("RESELLERCLUB_API_URL", "https://httpapi.com/api", is_secret=False)

    client = ResellerClubClient()

    assert client.reseller_id == "db-reseller"
    assert client.api_key == "db-key"
    assert client.base_url == "https://httpapi.com/api"


def test_extract_tlds_from_productkey_payload_values():
    client = ResellerClubClient()

    payload = [
        {"productkey": "com-domain", "name": ".COM"},
        {"product_key": "co.uk-domain"},
        {"product-key": "io-domain"},
        {"productkey": "ssl-cert"},
    ]

    tlds = client._extract_tlds_from_payload(payload)

    assert tlds == ["co.uk", "com", "io"]


def test_list_available_tlds_returns_curated_list_without_api_call(monkeypatch):
    """list_available_tlds() uses a built-in curated list; it must not call the API.

    ResellerClub/LogicBoxes has no TLD-discovery endpoint and all attempts to
    probe one will result in HTTP 404.  The curated list is the correct approach.
    """
    client = ResellerClubClient()

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("list_available_tlds must NOT call the API")

    monkeypatch.setattr(client, "_get", should_not_be_called)

    tlds = client.list_available_tlds()

    assert isinstance(tlds, list)
    assert len(tlds) > 50, "Expected a comprehensive curated TLD list"
    assert "com" in tlds
    assert "net" in tlds
    assert "co.uk" in tlds
    assert "io" in tlds


def test_list_domain_orders_parses_orderid_keyed_payload(monkeypatch):
    client = ResellerClubClient()

    def fake_get(endpoint, params=None):
        assert endpoint == "domains/search"
        return {
            "12345": {
                "domainname": "example.com",
                "currentstatus": "Active",
                "creationtime": 1735689600,
                "endtime": 1767225600,
                "recurring": True,
            }
        }

    monkeypatch.setattr(client, "_get", fake_get)

    rows = client.list_domain_orders(page_no=1, no_of_records=25, status="All")

    assert len(rows) == 1
    assert rows[0]["orderid"] == "12345"
    assert rows[0]["domainname"] == "example.com"
    assert rows[0]["expiry_date"] == "2026-01-01"


def test_list_domain_orders_normalizes_alternate_domain_fields(monkeypatch):
    client = ResellerClubClient()

    def fake_get(endpoint, params=None):
        assert endpoint == "domains/search"
        return {
            "12345": {
                "description": "Example.COM",
                "entityid": 12345,
                "status": "Active",
                "creationtime": 1735689600,
                "endtime": 1767225600,
            },
            "67890": {
                "domain-name": "other.example",
                "order-id": 67890,
                "current-status": "Active",
            },
        }

    monkeypatch.setattr(client, "_get", fake_get)

    rows = client.list_domain_orders(page_no=1, no_of_records=25, status="All")

    assert len(rows) == 2
    assert rows[0]["domainname"] == "example.com"
    assert rows[0]["orderid"] == "12345"
    assert rows[0]["currentstatus"] == "Active"
    assert rows[1]["domainname"] == "other.example"
    assert rows[1]["orderid"] == "67890"
    assert rows[1]["currentstatus"] == "Active"


def test_list_domain_orders_flattens_logicboxes_dotted_keys(monkeypatch):
    client = ResellerClubClient()

    def fake_get(endpoint, params=None):
        assert endpoint == "domains/search"
        assert "status" not in (params or {}) or params.get("status") != "All"
        return {
            "recsonpage": "1",
            "recsindb": "1",
            "1": {
                "orders.orderid": "99901",
                "entity.description": "Dotted.Example",
                "entity.currentstatus": "Active",
                "entity.customerid": "77",
                "orders.creationtime": 1735689600,
                "orders.endtime": 1767225600,
                "orders.ns1": "ns1.cyberask.co.uk",
                "orders.ns2": "ns2.cyberask.co.uk",
            },
        }

    monkeypatch.setattr(client, "_get", fake_get)

    rows = client.list_domain_orders(page_no=1, no_of_records=50, status="All")

    assert len(rows) == 1
    assert rows[0]["domainname"] == "dotted.example"
    assert rows[0]["orderid"] == "99901"
    assert rows[0]["currentstatus"] == "Active"
    assert rows[0]["customerid"] == "77"
    assert rows[0]["nameservers"] == ["ns1.cyberask.co.uk", "ns2.cyberask.co.uk"]
    assert rows[0]["expiry_date"] == "2026-01-01"


def test_list_all_domain_orders_omits_invalid_all_status(monkeypatch):
    client = ResellerClubClient()
    seen_params = []

    def fake_get(endpoint, params=None):
        seen_params.append(params or {})
        return {
            "recsonpage": "1",
            "recsindb": "1",
            "1": {
                "orders.orderid": "1",
                "entity.description": "a.com",
                "entity.currentstatus": "Active",
            },
        }

    monkeypatch.setattr(client, "_get", fake_get)

    rows = client.list_all_domain_orders(status="All", no_of_records=50, max_pages=1)

    assert rows[0]["domainname"] == "a.com"
    assert "status" not in seen_params[0]
