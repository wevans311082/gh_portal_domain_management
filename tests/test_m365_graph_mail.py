from unittest.mock import Mock, patch

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings
from django.urls import reverse

from apps.admin_tools.models import IntegrationSetting
from apps.notifications.email_backend import MicrosoftGraphEmailBackend


@pytest.mark.django_db
@override_settings(M365_GRAPH_FALLBACK_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_m365_backend_disabled_uses_fallback():
    IntegrationSetting.set_value("M365_GRAPH_ENABLED", "false", is_secret=False)
    message = EmailMultiAlternatives("Fallback test", "Body", "from@example.com", ["to@example.com"])

    sent = MicrosoftGraphEmailBackend().send_messages([message])

    assert sent == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].subject == "Fallback test"


@pytest.mark.django_db
def test_m365_backend_sends_graph_payload_for_html_and_attachments():
    IntegrationSetting.set_value("M365_GRAPH_ENABLED", "true", is_secret=False)
    IntegrationSetting.set_value("M365_GRAPH_TENANT_ID", "tenant")
    IntegrationSetting.set_value("M365_GRAPH_CLIENT_ID", "client")
    IntegrationSetting.set_value("M365_GRAPH_CLIENT_SECRET", "secret")
    IntegrationSetting.set_value("M365_GRAPH_BILLING_MAILBOX", "billing@cyberask.co.uk", is_secret=False)

    message = EmailMultiAlternatives(
        "Invoice ready",
        "Plain body",
        "from@example.com",
        ["client@example.com"],
        cc=["accounts@example.com"],
        headers={"X-CyberAsk-Mailbox-Purpose": "billing"},
    )
    message.attach_alternative("<p>HTML body</p>", "text/html")
    message.attach("invoice.txt", b"hello", "text/plain")

    with patch("apps.notifications.email_backend.MicrosoftGraphMailClient") as client_cls:
        client = Mock()
        client_cls.return_value = client
        sent = MicrosoftGraphEmailBackend().send_messages([message])

    assert sent == 1
    _, kwargs = client.send_mail.call_args
    assert kwargs["mailbox"] == "billing@cyberask.co.uk"
    payload = kwargs["message"]
    assert payload["body"] == {"contentType": "HTML", "content": "<p>HTML body</p>"}
    assert payload["toRecipients"][0]["emailAddress"]["address"] == "client@example.com"
    assert payload["ccRecipients"][0]["emailAddress"]["address"] == "accounts@example.com"
    assert payload["attachments"][0]["name"] == "invoice.txt"


@pytest.mark.django_db
def test_m365_admin_config_saves_settings(client, django_user_model):
    staff = django_user_model.objects.create_user(
        email="m365-admin@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

    response = client.post(
        reverse("admin_tools:m365_graph_config"),
        {
            "enabled": "on",
            "tenant_id": "tenant-id",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "default_mailbox": "notifications@cyberask.co.uk",
            "billing_mailbox": "billing@cyberask.co.uk",
            "support_mailbox": "support@cyberask.co.uk",
            "domains_mailbox": "domains@cyberask.co.uk",
            "notifications_mailbox": "notifications@cyberask.co.uk",
            "save_to_sent_items": "on",
            "timeout_seconds": "15",
            "action": "save",
        },
    )

    assert response.status_code == 302
    assert IntegrationSetting.get_value("M365_GRAPH_ENABLED") == "true"
    assert IntegrationSetting.get_value("M365_GRAPH_TENANT_ID") == "tenant-id"
    assert IntegrationSetting.get_value("M365_GRAPH_CLIENT_SECRET") == "client-secret"
    assert IntegrationSetting.get_value("M365_GRAPH_BILLING_MAILBOX") == "billing@cyberask.co.uk"
