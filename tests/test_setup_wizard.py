from pathlib import Path

import pytest
from tests.security_helpers import login_owner
from django.urls import reverse

from apps.admin_tools.models import WizardProgress


@pytest.mark.parametrize("address", ["192.0.2.44", "", "2001:db8::1", "invalid"])
def test_hosting_dns_address_requires_ipv4(address):
    from apps.admin_tools.wizard_views import HostingSettingsForm

    form = HostingSettingsForm(data={"whm_port": 2087, "whm_dns_zone_ip": address})
    assert form.is_valid() is (address in {"192.0.2.44", ""})


@pytest.mark.parametrize("nameservers", ["ns1.example.com,ns2.example.com", "", "ns1.example.com,NS1.example.com", "https://ns1.example.com"])
def test_hosting_dns_nameservers_are_validated(nameservers):
    from apps.admin_tools.wizard_views import HostingSettingsForm

    form = HostingSettingsForm(data={"whm_port": 2087, "whm_nameservers": nameservers})
    assert form.is_valid() is (nameservers in {"ns1.example.com,ns2.example.com", ""})


@pytest.mark.parametrize("account", ["a" * 32, "", "not-an-account"])
def test_cloudflare_zone_account_is_validated(account):
    from apps.admin_tools.wizard_views import CloudflareSettingsForm

    form = CloudflareSettingsForm(data={"cloudflare_account_id": account})
    assert form.is_valid() is (account in {"a" * 32, ""})


@pytest.mark.django_db
def test_finished_wizard_still_accessible(client, django_user_model):
    staff = django_user_model.objects.create_user(
        email="wizard-admin@example.com",
        password="password123",
        is_staff=True,
    )
    WizardProgress.objects.create(
        completed_steps=list(WizardProgress.STEPS),
        finished=True,
    )

    login_owner(client, staff)
    response = client.get(reverse("admin_tools:wizard_index"))

    assert response.status_code == 200
    assert "Setup is complete" in response.content.decode()


@pytest.mark.django_db
def test_registrar_step_save_uses_live_url_and_writes_customer_id(client, django_user_model, monkeypatch, tmp_path):
    from apps.admin_tools import wizard_views
    from apps.admin_tools.models import IntegrationSetting

    env_file = Path(tmp_path / ".env")
    monkeypatch.setattr(wizard_views.settings, "BASE_DIR", tmp_path)

    staff = django_user_model.objects.create_user(
        email="wizard-save@example.com",
        password="password123",
        is_staff=True,
    )
    login_owner(client, staff)

    response = client.post(
        reverse("admin_tools:wizard_step", kwargs={"step_key": WizardProgress.STEP_REGISTRAR}),
        {
            "action": "save",
            "resellerclub_reseller_id": "123456",
            "resellerclub_customer_id": "654321",
            "resellerclub_api_key": "top-secret",
            "resellerclub_api_mode": "live",
            "resellerclub_api_url": "https://ignore-this-for-live.example",
        },
    )

    assert response.status_code == 302
    assert not env_file.exists()
    assert IntegrationSetting.get_value("RESELLERCLUB_API_URL", "") == "https://httpapi.com/api"
    assert IntegrationSetting.get_value("RESELLERCLUB_CUSTOMER_ID", "") == "654321"


@pytest.mark.django_db
def test_registrar_step_test_connection_does_not_mark_step_done(client, django_user_model, monkeypatch):
    from apps.admin_tools import wizard_views

    monkeypatch.setattr(wizard_views, "_test_connection", lambda step_key, data: (True, "OK"))

    staff = django_user_model.objects.create_user(
        email="wizard-test@example.com",
        password="password123",
        is_staff=True,
    )
    login_owner(client, staff)

    response = client.post(
        reverse("admin_tools:wizard_step", kwargs={"step_key": WizardProgress.STEP_REGISTRAR}),
        {
            "action": "test",
            "resellerclub_reseller_id": "123456",
            "resellerclub_customer_id": "654321",
            "resellerclub_api_key": "top-secret",
            "resellerclub_api_mode": "test",
            "resellerclub_api_url": "https://test.httpapi.com/api",
        },
    )

    assert response.status_code == 200
    progress = WizardProgress.get_or_create_singleton()
    assert WizardProgress.STEP_REGISTRAR not in progress.completed_steps
    assert "Connection test passed" in response.content.decode()


@pytest.mark.django_db
def test_hosting_step_save_does_not_clear_existing_whm_token_when_blank(client, django_user_model, monkeypatch, tmp_path):
    from apps.admin_tools import wizard_views
    from apps.admin_tools.models import IntegrationSetting

    env_file = Path(tmp_path / ".env")
    monkeypatch.setattr(wizard_views.settings, "BASE_DIR", tmp_path)

    staff = django_user_model.objects.create_user(
        email="wizard-hosting-save@example.com",
        password="password123",
        is_staff=True,
    )
    login_owner(client, staff)

    IntegrationSetting.set_value("WHM_API_TOKEN", "saved-token", is_secret=True)
    env_file.write_text('WHM_API_TOKEN="saved-token"\n', encoding="utf-8")

    response = client.post(
        reverse("admin_tools:wizard_step", kwargs={"step_key": WizardProgress.STEP_HOSTING}),
        {
            "action": "save",
            "whm_host": "whm.example.com",
            "whm_port": "2087",
            "whm_username": "root",
            "whm_api_token": "",
            "whm_dns_zone_ip": "192.0.2.44",
            "whm_nameservers": "ns1.example.com,ns2.example.com",
        },
    )

    assert response.status_code == 302
    assert IntegrationSetting.get_value("WHM_API_TOKEN", "") == "saved-token"
    assert IntegrationSetting.get_value("WHM_DNS_ZONE_IP", "") == "192.0.2.44"
    assert IntegrationSetting.get_value("WHM_NAMESERVERS", "") == "ns1.example.com,ns2.example.com"
    env_text = env_file.read_text(encoding="utf-8")
    assert 'WHM_API_TOKEN="saved-token"' in env_text


@pytest.mark.django_db
def test_hosting_step_test_uses_saved_whm_token_when_form_token_blank(client, django_user_model, monkeypatch):
    from apps.admin_tools import wizard_views
    from apps.admin_tools.models import IntegrationSetting

    staff = django_user_model.objects.create_user(
        email="wizard-hosting-test@example.com",
        password="password123",
        is_staff=True,
    )
    login_owner(client, staff)

    IntegrationSetting.set_value("WHM_API_TOKEN", "saved-token", is_secret=True)

    class DummyResponse:
        status_code = 200
        text = "ok"

    def fake_get(url, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == "whm root:saved-token"
        return DummyResponse()

    monkeypatch.setattr(wizard_views.requests, "get", fake_get)

    response = client.post(
        reverse("admin_tools:wizard_step", kwargs={"step_key": WizardProgress.STEP_HOSTING}),
        {
            "action": "test",
            "whm_host": "whm.example.com",
            "whm_port": "2087",
            "whm_username": "root",
            "whm_api_token": "",
        },
    )

    assert response.status_code == 200
    assert "Connection test passed" in response.content.decode()


@pytest.mark.django_db
def test_registrar_connection_test_uses_saved_api_key_when_form_key_blank(monkeypatch):
    from apps.admin_tools import wizard_views
    from apps.admin_tools.models import IntegrationSetting

    IntegrationSetting.set_value("RESELLERCLUB_API_KEY", "saved-rc-key", is_secret=True)

    class DummyResponse:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {"status": "SUCCESS"}

    def fake_get(url, params=None, timeout=None):
        assert params["api-key"] == "saved-rc-key"
        return DummyResponse()

    monkeypatch.setattr(wizard_views.requests, "get", fake_get)

    ok, detail = wizard_views._test_connection(
        WizardProgress.STEP_REGISTRAR,
        {
            "resellerclub_api_url": "https://httpapi.com/api",
            "resellerclub_reseller_id": "123456",
            "resellerclub_api_key": "",
        },
    )

    assert ok is True
    assert "Connection OK" in detail


@pytest.mark.django_db
def test_cloudflare_connection_test_uses_saved_token_when_form_token_blank(monkeypatch):
    from apps.admin_tools import wizard_views
    from apps.admin_tools.models import IntegrationSetting

    IntegrationSetting.set_value("CLOUDFLARE_API_TOKEN", "saved-cf-token", is_secret=True)

    class DummyResponse:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {"success": True}

    def fake_get(url, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer saved-cf-token"
        return DummyResponse()

    monkeypatch.setattr(wizard_views.requests, "get", fake_get)

    ok, detail = wizard_views._test_connection(
        WizardProgress.STEP_CLOUDFLARE,
        {"cloudflare_api_token": ""},
    )

    assert ok is True
    assert "Connection OK" in detail


@pytest.mark.django_db
def test_payments_connection_test_uses_saved_tokens_when_form_tokens_blank(monkeypatch):
    from apps.admin_tools import wizard_views
    from apps.admin_tools.models import IntegrationSetting

    IntegrationSetting.set_value("STRIPE_SECRET_KEY", "saved-stripe", is_secret=True)
    IntegrationSetting.set_value("GOCARDLESS_ACCESS_TOKEN", "saved-gc", is_secret=True)

    class DummyResponse:
        status_code = 200
        text = "ok"

    def fake_get(url, auth=None, headers=None, timeout=None):
        if "stripe.com" in url:
            assert auth == ("saved-stripe", "")
        if "gocardless" in url:
            assert headers["Authorization"] == "Bearer saved-gc"
        return DummyResponse()

    monkeypatch.setattr(wizard_views.requests, "get", fake_get)

    ok, detail = wizard_views._test_connection(
        WizardProgress.STEP_PAYMENTS,
        {
            "stripe_secret_key": "",
            "gocardless_access_token": "",
            "gocardless_environment": "sandbox",
        },
    )

    assert ok is True
    assert "Stripe OK" in detail
    assert "GoCardless OK" in detail


@pytest.mark.django_db
def test_email_connection_test_uses_saved_password_when_form_password_blank(monkeypatch):
    from apps.admin_tools import wizard_views
    from apps.admin_tools.models import IntegrationSetting

    IntegrationSetting.set_value("EMAIL_HOST_PASSWORD", "saved-smtp-pass", is_secret=True)

    class DummySMTP:
        login_calls = []

        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port

        def ehlo(self):
            return None

        def starttls(self):
            return None

        def login(self, username, password):
            DummySMTP.login_calls.append((username, password))

        def quit(self):
            return None

    monkeypatch.setattr(wizard_views.smtplib, "SMTP", DummySMTP)

    ok, detail = wizard_views._test_connection(
        WizardProgress.STEP_EMAIL,
        {
            "email_host": "smtp.example.com",
            "email_port": 587,
            "email_use_tls": True,
            "email_host_user": "mailer@example.com",
            "email_host_password": "",
        },
    )

    assert ok is True
    assert "Connection OK" in detail
    assert DummySMTP.login_calls == [("mailer@example.com", "saved-smtp-pass")]
