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
