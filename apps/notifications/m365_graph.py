"""Microsoft 365 Graph mail integration."""
import base64
import logging
from dataclasses import dataclass
from typing import Iterable, Optional

import requests
from django.conf import settings

from apps.core.runtime_settings import get_runtime_bool, get_runtime_int, get_runtime_setting

logger = logging.getLogger(__name__)


class MicrosoftGraphMailError(Exception):
    """Raised when Microsoft Graph mail cannot be sent or tested."""


@dataclass
class MicrosoftGraphMailConfig:
    enabled: bool
    tenant_id: str
    client_id: str
    client_secret: str
    default_mailbox: str
    billing_mailbox: str
    support_mailbox: str
    domains_mailbox: str
    notifications_mailbox: str
    save_to_sent_items: bool
    timeout_seconds: int

    @classmethod
    def load(cls):
        return cls(
            enabled=get_runtime_bool("M365_GRAPH_ENABLED", getattr(settings, "M365_GRAPH_ENABLED", False)),
            tenant_id=get_runtime_setting("M365_GRAPH_TENANT_ID", getattr(settings, "M365_GRAPH_TENANT_ID", "")),
            client_id=get_runtime_setting("M365_GRAPH_CLIENT_ID", getattr(settings, "M365_GRAPH_CLIENT_ID", "")),
            client_secret=get_runtime_setting("M365_GRAPH_CLIENT_SECRET", getattr(settings, "M365_GRAPH_CLIENT_SECRET", "")),
            default_mailbox=get_runtime_setting("M365_GRAPH_DEFAULT_MAILBOX", getattr(settings, "M365_GRAPH_DEFAULT_MAILBOX", "")),
            billing_mailbox=get_runtime_setting("M365_GRAPH_BILLING_MAILBOX", getattr(settings, "M365_GRAPH_BILLING_MAILBOX", "")),
            support_mailbox=get_runtime_setting("M365_GRAPH_SUPPORT_MAILBOX", getattr(settings, "M365_GRAPH_SUPPORT_MAILBOX", "")),
            domains_mailbox=get_runtime_setting("M365_GRAPH_DOMAINS_MAILBOX", getattr(settings, "M365_GRAPH_DOMAINS_MAILBOX", "")),
            notifications_mailbox=get_runtime_setting(
                "M365_GRAPH_NOTIFICATIONS_MAILBOX",
                getattr(settings, "M365_GRAPH_NOTIFICATIONS_MAILBOX", ""),
            ),
            save_to_sent_items=get_runtime_bool(
                "M365_GRAPH_SAVE_TO_SENT_ITEMS",
                getattr(settings, "M365_GRAPH_SAVE_TO_SENT_ITEMS", True),
            ),
            timeout_seconds=get_runtime_int("M365_GRAPH_TIMEOUT_SECONDS", getattr(settings, "M365_GRAPH_TIMEOUT_SECONDS", 15)),
        )

    def mailbox_for(self, purpose: str = "") -> str:
        purpose = (purpose or "").strip().lower()
        mapping = {
            "billing": self.billing_mailbox,
            "payments": self.billing_mailbox,
            "quotes": self.billing_mailbox,
            "support": self.support_mailbox,
            "domains": self.domains_mailbox,
            "notifications": self.notifications_mailbox,
        }
        return mapping.get(purpose) or self.default_mailbox

    def validate(self, *, purpose: str = ""):
        missing = []
        for field_name in ("tenant_id", "client_id", "client_secret"):
            if not getattr(self, field_name):
                missing.append(field_name)
        if not self.mailbox_for(purpose):
            missing.append(f"{purpose or 'default'} mailbox")
        if missing:
            raise MicrosoftGraphMailError("Microsoft 365 Graph mail is missing: " + ", ".join(missing))


class MicrosoftGraphMailClient:
    token_url_template = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    graph_base_url = "https://graph.microsoft.com/v1.0"

    def __init__(self, config: Optional[MicrosoftGraphMailConfig] = None):
        self.config = config or MicrosoftGraphMailConfig.load()

    def get_access_token(self) -> str:
        self.config.validate()
        response = requests.post(
            self.token_url_template.format(tenant_id=self.config.tenant_id),
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=self.config.timeout_seconds,
        )
        if response.status_code >= 400:
            raise MicrosoftGraphMailError(f"Token request failed ({response.status_code}): {response.text[:500]}")
        token = response.json().get("access_token")
        if not token:
            raise MicrosoftGraphMailError("Token response did not contain access_token")
        return token

    def _headers(self):
        return {"Authorization": f"Bearer {self.get_access_token()}", "Content-Type": "application/json"}

    def test_mailbox(self, mailbox: str) -> dict:
        if not mailbox:
            raise MicrosoftGraphMailError("Mailbox is required for connectivity test")
        response = requests.get(
            f"{self.graph_base_url}/users/{mailbox}",
            headers=self._headers(),
            params={"$select": "id,displayName,mail,userPrincipalName"},
            timeout=self.config.timeout_seconds,
        )
        if response.status_code >= 400:
            raise MicrosoftGraphMailError(f"Mailbox test failed ({response.status_code}): {response.text[:500]}")
        return response.json()

    def send_mail(self, *, mailbox: str, message: dict, save_to_sent_items: Optional[bool] = None):
        if not mailbox:
            raise MicrosoftGraphMailError("Mailbox is required to send mail")
        payload = {
            "message": message,
            "saveToSentItems": self.config.save_to_sent_items if save_to_sent_items is None else bool(save_to_sent_items),
        }
        response = requests.post(
            f"{self.graph_base_url}/users/{mailbox}/sendMail",
            headers=self._headers(),
            json=payload,
            timeout=self.config.timeout_seconds,
        )
        if response.status_code >= 400:
            raise MicrosoftGraphMailError(f"sendMail failed ({response.status_code}): {response.text[:500]}")
        return {"ok": True, "status_code": response.status_code}


def address_list(addresses: Optional[Iterable[str]]) -> list[dict]:
    return [{"emailAddress": {"address": str(address).strip()}} for address in addresses or [] if str(address).strip()]


def attachment_payload(filename: str, content, mimetype: str = "") -> dict:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": filename,
        "contentType": mimetype or "application/octet-stream",
        "contentBytes": base64.b64encode(content or b"").decode("ascii"),
    }
