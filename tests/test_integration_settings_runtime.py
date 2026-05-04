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


def test_list_available_tlds_uses_first_successful_catalog(monkeypatch):
    client = ResellerClubClient()
    calls = []

    def fake_get(endpoint, params=None):
        calls.append(endpoint)
        if endpoint == "products/list":
            return [{"productkey": "com-domain"}, {"productkey": "net-domain"}]
        raise AssertionError("Fallback endpoint should not be called after successful extraction")

    monkeypatch.setattr(client, "_get", fake_get)

    tlds = client.list_available_tlds()

    assert tlds == ["com", "net"]
    assert calls == ["products/list"]
