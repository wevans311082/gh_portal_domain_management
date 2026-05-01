"""
Tests for:
  - apps/notifications/services.py  (send_notification, EmailLog)
  - apps/cloudflare_integration/services.py  (CloudflareService HTTP calls)
  - apps/companies/services.py  (CompaniesHouseService HTTP calls)
  - apps/core/middleware.py  (RequestCorrelationIDMiddleware, ContentSecurityPolicyMiddleware)
"""
import json
import pytest
from unittest.mock import patch, MagicMock
from django.test import RequestFactory, override_settings
from django.urls import reverse


# ─────────────────────────────────────────────
# Notification service
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_send_notification_sends_email(django_user_model, mailoutbox):
    from apps.notifications.services import send_notification

    user = django_user_model.objects.create_user(email="notify@test.com", password="x")
    send_notification("welcome", user, {"site_name": "Grumpy Hosting"})

    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == ["notify@test.com"]
    assert "Welcome" in mailoutbox[0].subject


@pytest.mark.django_db
def test_send_notification_unknown_template_no_email_sent(django_user_model, mailoutbox):
    from apps.notifications.services import send_notification

    user = django_user_model.objects.create_user(email="warn@test.com", password="x")
    send_notification("nonexistent_template", user)

    # Unknown template → no email dispatched
    assert len(mailoutbox) == 0


@pytest.mark.django_db
def test_send_notification_subject_formatted(django_user_model, mailoutbox):
    from apps.notifications.services import send_notification

    user = django_user_model.objects.create_user(email="sub@test.com", password="x")
    send_notification("invoice_issued", user, {
        "invoice_number": "INV-9999",
        "site_name": "Grumpy Hosting",
    })

    assert len(mailoutbox) == 1
    assert "INV-9999" in mailoutbox[0].subject


@pytest.mark.django_db
def test_send_notification_records_email_log(django_user_model, mailoutbox):
    from apps.notifications.services import send_notification
    from apps.audit.models import EmailLog

    user = django_user_model.objects.create_user(email="log@test.com", password="x")
    send_notification("welcome", user, {"site_name": "Grumpy Hosting"})

    log = EmailLog.objects.filter(recipient="log@test.com").first()
    assert log is not None
    assert log.status in ("sent", "delivered")


@pytest.mark.django_db
def test_send_notification_records_failure_log_on_error(django_user_model):
    from apps.notifications.services import send_notification
    from apps.audit.models import EmailLog

    user = django_user_model.objects.create_user(email="fail@test.com", password="x")

    with patch("apps.notifications.services.EmailMultiAlternatives.send", side_effect=Exception("SMTP down")):
        send_notification("welcome", user, {"site_name": "Grumpy Hosting"})

    log = EmailLog.objects.filter(recipient="fail@test.com").first()
    assert log is not None
    assert log.status == "failed"
    assert "SMTP down" in log.error


@pytest.mark.django_db
def test_send_notification_domain_expiry_subject(django_user_model, mailoutbox):
    from apps.notifications.services import send_notification

    user = django_user_model.objects.create_user(email="expiry@test.com", password="x")
    send_notification("domain_expiry_reminder", user, {
        "domain": "example.com",
        "site_name": "Grumpy Hosting",
    })

    assert len(mailoutbox) == 1
    assert "example.com" in mailoutbox[0].subject


# ─────────────────────────────────────────────
# Cloudflare service
# ─────────────────────────────────────────────

@override_settings(CLOUDFLARE_API_TOKEN="test-token")
def test_cloudflare_create_zone_calls_api():
    from apps.cloudflare_integration.services import CloudflareService

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": {"id": "zone123", "name_servers": []}, "success": True}
    mock_resp.raise_for_status = MagicMock()

    with patch("apps.cloudflare_integration.services.requests.request", return_value=mock_resp) as mock_req:
        svc = CloudflareService()
        result = svc.create_zone("example.com")

    mock_req.assert_called_once()
    call_kwargs = mock_req.call_args
    assert "zones" in call_kwargs[0][1]
    assert result["result"]["id"] == "zone123"


@override_settings(CLOUDFLARE_API_TOKEN="test-token")
def test_cloudflare_create_dns_record():
    from apps.cloudflare_integration.services import CloudflareService

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": {"id": "rec456"}, "success": True}
    mock_resp.raise_for_status = MagicMock()

    with patch("apps.cloudflare_integration.services.requests.request", return_value=mock_resp) as mock_req:
        svc = CloudflareService()
        result = svc.create_dns_record("zone123", "A", "example.com", "1.2.3.4")

    assert result["result"]["id"] == "rec456"
    args = mock_req.call_args
    assert "dns_records" in args[0][1]


@override_settings(CLOUDFLARE_API_TOKEN="test-token")
def test_cloudflare_delete_dns_record():
    from apps.cloudflare_integration.services import CloudflareService

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"success": True}
    mock_resp.raise_for_status = MagicMock()

    with patch("apps.cloudflare_integration.services.requests.request", return_value=mock_resp) as mock_req:
        svc = CloudflareService()
        svc.delete_dns_record("zone123", "rec456")

    method = mock_req.call_args[0][0]
    assert method == "DELETE"


@override_settings(CLOUDFLARE_API_TOKEN="test-token")
def test_cloudflare_http_error_propagates():
    from apps.cloudflare_integration.services import CloudflareService
    import requests as req_lib

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = req_lib.HTTPError("403 Forbidden")

    with patch("apps.cloudflare_integration.services.requests.request", return_value=mock_resp):
        svc = CloudflareService()
        with pytest.raises(req_lib.HTTPError):
            svc.get_zone("bad-zone")


# ─────────────────────────────────────────────
# Companies House service
# ─────────────────────────────────────────────

@override_settings(COMPANIES_HOUSE_API_KEY="ch-key")
def test_companies_house_get_company_success():
    from apps.companies.services import CompaniesHouseService

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"company_number": "12345678", "company_name": "Acme Ltd"}

    with patch("apps.companies.services.requests.get", return_value=mock_resp):
        svc = CompaniesHouseService()
        result = svc.get_company("12345678")

    assert result["company_number"] == "12345678"


@override_settings(COMPANIES_HOUSE_API_KEY="ch-key")
def test_companies_house_get_company_not_found():
    from apps.companies.services import CompaniesHouseService

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("apps.companies.services.requests.get", return_value=mock_resp):
        svc = CompaniesHouseService()
        result = svc.get_company("00000000")

    assert result is None


@override_settings(COMPANIES_HOUSE_API_KEY="ch-key")
def test_companies_house_search():
    from apps.companies.services import CompaniesHouseService

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"items": [{"company_name": "Acme Ltd"}], "total_results": 1}

    with patch("apps.companies.services.requests.get", return_value=mock_resp):
        svc = CompaniesHouseService()
        result = svc.search_companies("Acme")

    assert result["total_results"] == 1


# ─────────────────────────────────────────────
# Core middleware — RequestCorrelationIDMiddleware
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_correlation_id_generated_when_absent(client):
    response = client.get(reverse("accounts_custom:login"))
    assert "X-Request-ID" in response
    assert len(response["X-Request-ID"]) == 32  # uuid4().hex


@pytest.mark.django_db
def test_correlation_id_propagated_from_header(client):
    response = client.get(
        reverse("accounts_custom:login"),
        HTTP_X_REQUEST_ID="my-upstream-id-12345",
    )
    assert response["X-Request-ID"] == "my-upstream-id-12345"


# ─────────────────────────────────────────────
# Core middleware — ContentSecurityPolicyMiddleware
# ─────────────────────────────────────────────

@pytest.mark.django_db
@override_settings(
    CSP_DEFAULT_SRC=["'self'"],
    CSP_SCRIPT_SRC=["'self'", "cdn.example.com"],
    CSP_STYLE_SRC=[],
    CSP_IMG_SRC=None,
    CSP_FONT_SRC=None,
    CSP_CONNECT_SRC=None,
    CSP_FRAME_ANCESTORS=["'none'"],
)
def test_csp_header_built_from_settings(client):
    response = client.get(reverse("accounts_custom:login"))
    csp = response.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self' cdn.example.com" in csp
    assert "frame-ancestors 'none'" in csp
    # Empty list / None directives are omitted
    assert "style-src" not in csp


@pytest.mark.django_db
@override_settings(
    CSP_DEFAULT_SRC=None,
    CSP_SCRIPT_SRC=None,
    CSP_STYLE_SRC=None,
    CSP_IMG_SRC=None,
    CSP_FONT_SRC=None,
    CSP_CONNECT_SRC=None,
    CSP_FRAME_ANCESTORS=None,
)
def test_csp_header_absent_when_no_directives(client):
    response = client.get(reverse("accounts_custom:login"))
    assert "Content-Security-Policy" not in response
