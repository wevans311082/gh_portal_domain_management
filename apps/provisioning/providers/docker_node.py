"""Placeholder provider for the upcoming Ubuntu Docker node daemon."""
from __future__ import annotations

from typing import Any

from apps.provisioning.nginx import NginxSiteConfigError, NginxSiteConfigService, NginxTlsOptions
from apps.provisioning.providers.base import ProvisioningProvider, ProvisioningProviderError


class DockerNodeProvider(ProvisioningProvider):
    """Future provider for provisioning sites through the Ubuntu daemon workflow."""

    provider_key = "docker_node"
    display_name = "Docker node daemon"

    def _not_ready(self) -> None:
        daemon_url = self.config.get("daemon_url", "")
        detail = f" Docker node daemon URL: {daemon_url}." if daemon_url else ""
        raise ProvisioningProviderError(
            "DockerNodeProvider is a placeholder and is not ready for provisioning yet." + detail
        )

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
        self._not_ready()

    def suspend_site(self, *, username: str, reason: str = "") -> dict[str, Any]:
        self._not_ready()

    def delete_site(self, *, username: str, keep_dns: bool = False) -> dict[str, Any]:
        self._not_ready()

    def create_mail_domain(self, *, username: str, domain: str) -> dict[str, Any]:
        self._not_ready()

    def create_backup(self, *, username: str) -> dict[str, Any]:
        self._not_ready()

    def configure_site_vhost(
        self,
        *,
        domain: str,
        container_name: str,
        internal_port: int,
        tls_enabled: bool = False,
        tls_certificate_path: str = "",
        tls_certificate_key_path: str = "",
        force_https: bool = False,
    ) -> dict[str, Any]:
        """Render, validate, and activate nginx routing for a Docker-hosted site."""
        service = NginxSiteConfigService(
            template_path=self.config.get("nginx_template_path"),
            sites_dir=self.config.get("nginx_sites_dir"),
            validate_command=self.config.get("nginx_validate_command"),
            reload_command=self.config.get("nginx_reload_command"),
        )
        try:
            destination = service.apply_site_config(
                domain=domain,
                container_name=container_name,
                internal_port=internal_port,
                tls=NginxTlsOptions(
                    enabled=tls_enabled,
                    certificate_path=tls_certificate_path,
                    certificate_key_path=tls_certificate_key_path,
                    force_https=force_https,
                ),
            )
        except NginxSiteConfigError as exc:
            raise ProvisioningProviderError(str(exc)) from exc
        return {
            "ok": True,
            "provider": self.provider_key,
            "domain": domain,
            "config_path": str(destination),
        }

    def health_check(self) -> dict[str, Any]:
        return {"ok": False, "provider": self.provider_key, "ready": False}
