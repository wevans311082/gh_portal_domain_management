"""WHM/cPanel provisioning provider implementation."""
from __future__ import annotations

from typing import Any

from apps.provisioning.providers.base import ProvisioningProvider, ProvisioningProviderError
from apps.provisioning.whm_client import WHMClient, WHMClientError


class WHMProvider(ProvisioningProvider):
    """Provision hosting resources through the existing WHM/cPanel client."""

    provider_key = "whm"
    display_name = "WHM/cPanel"

    def __init__(self, config: dict[str, Any] | None = None, client: WHMClient | None = None):
        super().__init__(config=config)
        self.client = client or WHMClient()

    def create_site(
        self,
        *,
        domain: str,
        username: str,
        password: str,
        package: str,
        email: str,
        service: Any | None = None,
    ) -> dict[str, Any]:
        try:
            return self.client.create_account(
                domain=domain,
                username=username,
                password=password,
                package=package,
                email=email,
            )
        except WHMClientError as exc:
            raise ProvisioningProviderError(str(exc)) from exc

    def suspend_site(self, *, username: str, reason: str = "") -> dict[str, Any]:
        try:
            return self.client.suspend_account(username=username, reason=reason)
        except WHMClientError as exc:
            raise ProvisioningProviderError(str(exc)) from exc

    def delete_site(self, *, username: str, keep_dns: bool = False) -> dict[str, Any]:
        try:
            return self.client.terminate_account(username=username, keep_dns=keep_dns)
        except WHMClientError as exc:
            raise ProvisioningProviderError(str(exc)) from exc

    def create_mail_domain(self, *, username: str, domain: str) -> dict[str, Any]:
        # cPanel automatically makes the primary account domain available for
        # mailbox creation during createacct. Keep this as an explicit provider
        # hook for backends where mail domains are separate resources.
        return {"ok": True, "provider": self.provider_key, "domain": domain, "created": False}

    def create_backup(self, *, username: str) -> dict[str, Any]:
        try:
            return self.client._call("create_user_backup", {"user": username})
        except WHMClientError as exc:
            raise ProvisioningProviderError(str(exc)) from exc

    def health_check(self) -> dict[str, Any]:
        try:
            return {"ok": True, "provider": self.provider_key, "result": self.client._call("version")}
        except WHMClientError as exc:
            raise ProvisioningProviderError(str(exc)) from exc
