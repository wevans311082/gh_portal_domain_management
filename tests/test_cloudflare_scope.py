from unittest.mock import patch

import pytest

from apps.cloudflare_integration.services import CloudflareService, CloudflareError
from apps.cloudflare_integration.scope import provider_scope, assert_provider_scope
from apps.admin_tools.setting_schema import validate_setting, SettingValidationError


def provider(account="a" * 32, token="PRIVATE-TOKEN"):
    with patch("apps.cloudflare_integration.services.get_runtime_setting", side_effect=lambda key, default: {
        "CLOUDFLARE_ACCOUNT_ID": account, "CLOUDFLARE_API_TOKEN": token}[key]):
        return CloudflareService()


def test_scope_is_stable_private_and_uses_captured_configuration_without_http():
    client = provider()
    with patch("apps.cloudflare_integration.services.requests.request") as http:
        first = provider_scope(client)
        assert first == provider_scope(provider()) and len(first) == 64
        with patch("apps.cloudflare_integration.services.get_runtime_setting", side_effect=AssertionError("Use the captured client")):
            assert_provider_scope(first, client)
    assert "PRIVATE" not in first and "a" * 32 not in first
    http.assert_not_called()


@pytest.mark.parametrize("change", ["account", "token", "header", "shadow-header", "endpoint"])
def test_configuration_rotation_or_header_drift_invalidates_old_request(change):
    client = provider()
    original = provider_scope(client)
    if change == "account":
        client.account_id = "b" * 32
    elif change == "token":
        client.token = "ROTATED-TOKEN"
        client.headers["Authorization"] = "Bearer ROTATED-TOKEN"
    elif change == "header":
        client.headers["Authorization"] = "Bearer OTHER-TOKEN"
    elif change == "shadow-header":
        client.headers["authorization"] = "Bearer OTHER-TOKEN"
    else:
        client.BASE_URL = "https://different.example.test/client/v4"
    with pytest.raises(CloudflareError) as error:
        assert_provider_scope(original, client)
    assert "TOKEN" not in str(error.value)


@pytest.mark.parametrize("account", ["", "bad/account", True])
def test_missing_or_invalid_account_cannot_become_job_authority(account):
    with pytest.raises(CloudflareError):
        provider_scope(provider(account=account))


@pytest.mark.parametrize("token", ["", "PRIVATE\nTOKEN", "PRIVATE TOKEN", True])
def test_invalid_token_cannot_become_job_authority(token):
    with pytest.raises(CloudflareError):
        provider_scope(provider(token=token))


def test_unrecognized_retained_scope_is_rejected():
    with pytest.raises(CloudflareError):
        assert_provider_scope("unknown", provider())


def test_account_setting_is_validated_and_is_not_a_secret():
    assert validate_setting("CLOUDFLARE_ACCOUNT_ID", "a" * 32) == ("a" * 32, False)
    with pytest.raises(SettingValidationError):
        validate_setting("CLOUDFLARE_ACCOUNT_ID", "not-an-account")
