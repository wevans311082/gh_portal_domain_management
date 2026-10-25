"""Password reset, customer mail, and staff alert delivery."""
import pytest
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from apps.admin_tools.forms import Microsoft365GraphSettingsForm
from apps.admin_tools.models import IntegrationSetting
from apps.audit.models import EmailLog
from apps.notifications.email_backend import MicrosoftGraphEmailBackend, outbound_mail_mode
from apps.notifications.m365_graph import MicrosoftGraphMailError


@pytest.mark.django_db
def test_password_reset_matches_mixed_case_and_is_logged(client, django_user_model):
    django_user_model.objects.create_user(email="Paul.Lee@Example.com", password="password123")

    response = client.post(reverse("account_reset_password"), {"email": "paul.lee@example.com"})

    assert response.status_code == 302
    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    assert "http" in body
    assert mail.outbox[0].extra_headers["X-CyberAsk-Mailbox-Purpose"] == "notifications"
    log = EmailLog.objects.get(recipient="paul.lee@example.com")
    assert log.status == "sent"
    assert log.template == "account/email/password_reset_key"


@pytest.mark.django_db
@override_settings(
    EMAIL_ALLOW_CONSOLE_FALLBACK=False,
    EMAIL_HOST="",
    M365_GRAPH_FALLBACK_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
)
def test_graph_failure_is_not_reported_as_console_delivery():
    cache.clear()
    IntegrationSetting.set_value("M365_GRAPH_ENABLED", "true", is_secret=False)
    IntegrationSetting.set_value("M365_GRAPH_TENANT_ID", "tenant", is_secret=False)
    IntegrationSetting.set_value("M365_GRAPH_CLIENT_ID", "client", is_secret=False)
    IntegrationSetting.set_value("M365_GRAPH_CLIENT_SECRET", "secret", is_secret=False)
    IntegrationSetting.set_value("M365_GRAPH_DEFAULT_MAILBOX", "notifications@cyberask.co.uk", is_secret=False)
    message = mail.EmailMultiAlternatives("Invoice ready", "Body", "from@example.com", ["client@example.com"])

    from unittest.mock import Mock, patch

    with patch("apps.notifications.email_backend.MicrosoftGraphMailClient") as client_cls:
        client = Mock()
        client.send_mail.side_effect = MicrosoftGraphMailError("denied")
        client_cls.return_value = client
        with pytest.raises(MicrosoftGraphMailError):
            MicrosoftGraphEmailBackend().send_messages([message])

    assert mail.outbox == []


@pytest.mark.django_db
@override_settings(M365_GRAPH_ENABLED=True, M365_GRAPH_DEFAULT_MAILBOX="ops@cyberask.co.uk")
def test_m365_form_initial_uses_environment_when_database_is_empty():
    cache.clear()
    initial = Microsoft365GraphSettingsForm.initial_from_settings()

    assert initial["enabled"] is True
    assert initial["default_mailbox"] == "ops@cyberask.co.uk"
    assert initial["client_secret"] == ""


@pytest.mark.django_db
@override_settings(EMAIL_ALLOW_CONSOLE_FALLBACK=False, EMAIL_HOST="", M365_GRAPH_ENABLED=False)
def test_outbound_mail_mode_is_unconfigured_without_graph_or_smtp():
    cache.clear()
    IntegrationSetting.set_value("M365_GRAPH_ENABLED", "false", is_secret=False)
    assert outbound_mail_mode() == "unconfigured"


@pytest.mark.django_db
def test_low_registrar_balance_emails_staff(django_user_model, monkeypatch):
    django_user_model.objects.create_user(email="ops@example.com", password="x", is_staff=True)
    django_user_model.objects.create_user(email="customer@example.com", password="x", is_staff=False)

    class FakeClient:
        def _get(self, *_args, **_kwargs):
            return {"sellingcurrencybalance": "1.00"}

    monkeypatch.setattr("apps.domains.tasks.ResellerClubClient", FakeClient)
    monkeypatch.setattr(
        "apps.core.runtime_settings.get_runtime_setting",
        lambda key, default="": "50.00",
    )

    from apps.domains.tasks import monitor_registrar_balance

    result = monitor_registrar_balance()

    assert result["alert_sent"] is True
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["ops@example.com"]
    assert "1.00" in mail.outbox[0].body or "1.00" in mail.outbox[0].alternatives[0][0]
