from pathlib import Path

import pytest
from django.urls import reverse

from apps.admin_tools.models import WizardProgress


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

    client.force_login(staff)
    response = client.get(reverse("admin_tools:wizard_index"))

    assert response.status_code == 200
    assert "Setup is complete" in response.content.decode()


@pytest.mark.django_db
def test_registrar_step_save_uses_live_url_and_writes_customer_id(client, django_user_model, monkeypatch, tmp_path):
    from apps.admin_tools import wizard_views
    from apps.admin_tools.models import IntegrationSetting

    env_file = Path(tmp_path / ".env")
    monkeypatch.setattr(wizard_views, "_ENV_PATH", env_file)

    staff = django_user_model.objects.create_user(
        email="wizard-save@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

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
    env_text = env_file.read_text(encoding="utf-8")
    assert 'RESELLERCLUB_API_URL="https://httpapi.com/api"' in env_text
    assert 'RESELLERCLUB_CUSTOMER_ID="654321"' in env_text
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
    client.force_login(staff)

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
