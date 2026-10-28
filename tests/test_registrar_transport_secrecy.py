from unittest.mock import Mock, patch

import pytest
import requests

from apps.domains.debug_state import get_entries, reset_entries
from apps.domains.resellerclub_client import ResellerClubClient, ResellerClubError, _build_session

SECRET = "PRIVATE-EPP-AND-API-KEY"


def client_with(response):
    client = object.__new__(ResellerClubClient)
    client.api_key, client.reseller_id, client.base_url = SECRET, "999", "https://provider.example/api"
    client._auth_params = {"api-key": SECRET, "auth-userid": "999"}
    client.session = Mock()
    client.session.get.return_value = client.session.post.return_value = response
    return client


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("outcome", ["http", "network", "business", "invalid", "redirect"])
def test_transport_never_retains_or_logs_provider_body_or_exception(method, outcome, caplog):
    response = Mock(status_code=200, text=SECRET)
    response.json.return_value = {"entityid": "501"}
    if outcome == "http":
        response.status_code = 500
        response.raise_for_status.side_effect = requests.HTTPError(SECRET, response=response)
    elif outcome == "business":
        response.json.return_value = {"status": "ERROR", "message": SECRET}
    elif outcome == "invalid":
        response.json.side_effect = ValueError(SECRET)
    elif outcome == "redirect":
        response.status_code = 307
    client = client_with(response)
    if outcome == "network":
        getattr(client.session, method.lower()).side_effect = requests.Timeout(SECRET)
    reset_entries()
    with patch("apps.domains.resellerclub_client.get_runtime_setting", return_value="true"), pytest.raises(ResellerClubError) as error:
        client._request(method, "domains/transfer" if method == "POST" else "domains/details", {"auth-code": SECRET})
    assert SECRET not in str(error.value) + str(get_entries()) + caplog.text
    assert getattr(client.session, method.lower()).call_count == 1
    assert getattr(client.session, method.lower()).call_args.kwargs["allow_redirects"] is False


def test_success_debug_capture_contains_metadata_only_and_preserves_typed_boolean_response():
    response = Mock(status_code=200, text=SECRET)
    response.json.return_value = True
    client = client_with(response)
    reset_entries()
    with patch("apps.domains.resellerclub_client.get_runtime_setting", return_value="true"):
        assert client._post("domains/raa/resend-verification", {"auth-code": SECRET, "auto-renew": False}) is True
    assert get_entries() == [{"request": {"method": "POST", "endpoint": "domains/raa/resend-verification.json"},
                              "response": {"status_code": 200}, "error": ""}]
    assert client.session.post.call_args.kwargs["data"]["auto-renew"] == "false"


def test_mutations_are_excluded_from_http_status_and_read_retries():
    session = _build_session()
    retries = session.get_adapter("https://provider.example").max_retries
    assert "POST" not in retries.allowed_methods
    assert not retries.is_retry("POST", 503)
    assert not retries._is_method_retryable("POST")
    session.close()


@pytest.mark.django_db
@pytest.mark.parametrize("failed", [False, True])
def test_owner_diagnostics_excludes_prepared_credentials_and_provider_response(client, django_user_model, failed):
    from django.urls import reverse
    from tests.security_helpers import login_owner
    owner = django_user_model.objects.create_user(email="safe-debug-owner@example.test", password="testpass")
    login_owner(client, owner)
    session = Mock()
    session.prepare_request.return_value.method = "GET"
    session.prepare_request.return_value.url = "https://provider.example/?api-key=" + SECRET
    session.send.return_value.status_code = 200
    session.send.return_value.text = SECRET
    session.send.return_value.headers = {"X-Private": SECRET}
    if failed:
        session.send.side_effect = requests.Timeout(SECRET)
    with patch("apps.admin_tools.views.requests.Session", return_value=session):
        response = client.post(reverse("admin_tools:resellerclub_debug"), {"raw_params": '{"auth-code":"' + SECRET + '"}'})
    assert response.status_code == 200 and SECRET not in response.content.decode()
    assert session.send.call_args.kwargs["allow_redirects"] is False
