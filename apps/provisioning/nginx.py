"""Helpers for rendering and activating per-site nginx virtual hosts."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from django.conf import settings
from django.template import Context, Engine


class NginxSiteConfigError(Exception):
    """Raised when a per-site nginx config cannot be activated."""


_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}$")


@dataclass(frozen=True)
class NginxTlsOptions:
    """TLS options for a customer website vhost."""

    enabled: bool = False
    certificate_path: str = ""
    certificate_key_path: str = ""
    force_https: bool = False


class NginxSiteConfigService:
    """Render, validate, and activate per-site nginx config files."""

    def __init__(
        self,
        *,
        template_path: str | Path | None = None,
        sites_dir: str | Path | None = None,
        validate_command: Sequence[str] | None = None,
        reload_command: Sequence[str] | None = None,
    ) -> None:
        base_dir = Path(settings.BASE_DIR)
        self.template_path = Path(template_path or base_dir / "nginx" / "templates" / "site.conf.j2")
        self.sites_dir = Path(sites_dir or base_dir / "nginx" / "conf.d" / "sites")
        self.validate_command = list(validate_command or ["nginx", "-t"])
        self.reload_command = list(reload_command or ["nginx", "-s", "reload"])

    def render_site_config(
        self,
        *,
        domain: str,
        container_name: str,
        internal_port: int,
        tls: NginxTlsOptions | None = None,
    ) -> str:
        """Return rendered nginx config text for a customer website."""
        clean_domain = self._validate_domain(domain)
        clean_container = self._validate_container_name(container_name)
        clean_port = self._validate_internal_port(internal_port)
        clean_tls = self._validate_tls_options(tls or NginxTlsOptions())

        template = Engine(debug=getattr(settings, "DEBUG", False)).from_string(
            self.template_path.read_text(encoding="utf-8")
        )
        return template.render(
            Context(
                {
                    "server_name": clean_domain,
                    "container_name": clean_container,
                    "internal_port": clean_port,
                    "tls": {
                        "enabled": clean_tls.enabled,
                        "certificate_path": clean_tls.certificate_path,
                        "certificate_key_path": clean_tls.certificate_key_path,
                        "force_https": clean_tls.force_https,
                    },
                },
                autoescape=False,
            )
        )

    def apply_site_config(
        self,
        *,
        domain: str,
        container_name: str,
        internal_port: int,
        tls: NginxTlsOptions | None = None,
    ) -> Path:
        """Write a per-site config, validate nginx, then reload nginx if validation passes."""
        rendered_config = self.render_site_config(
            domain=domain,
            container_name=container_name,
            internal_port=internal_port,
            tls=tls,
        )
        clean_domain = self._validate_domain(domain)
        destination = self.sites_dir / f"{clean_domain}.conf"
        self.sites_dir.mkdir(parents=True, exist_ok=True)

        previous_config = destination.read_text(encoding="utf-8") if destination.exists() else None
        self._atomic_write(destination, rendered_config)

        try:
            self._run_command(self.validate_command, "nginx validation")
        except NginxSiteConfigError:
            if previous_config is None:
                destination.unlink(missing_ok=True)
            else:
                self._atomic_write(destination, previous_config)
            raise

        self._run_command(self.reload_command, "nginx reload")
        return destination

    def _run_command(self, command: Sequence[str], label: str) -> None:
        if not command:
            raise NginxSiteConfigError(f"Missing {label} command.")
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
            raise NginxSiteConfigError(f"{label} failed: {output or 'no output'}")

    def _atomic_write(self, destination: Path, content: str) -> None:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as tmp_file:
            tmp_file.write(content)
            tmp_name = tmp_file.name
        os.replace(tmp_name, destination)

    def _validate_domain(self, domain: str) -> str:
        clean_domain = domain.strip().lower().rstrip(".")
        if not _DOMAIN_RE.match(clean_domain):
            raise NginxSiteConfigError("Domain must be a valid fully-qualified domain name.")
        return clean_domain

    def _validate_container_name(self, container_name: str) -> str:
        clean_container = container_name.strip()
        if not _CONTAINER_RE.match(clean_container):
            raise NginxSiteConfigError("Container name contains unsupported characters.")
        return clean_container

    def _validate_internal_port(self, internal_port: int) -> int:
        try:
            clean_port = int(internal_port)
        except (TypeError, ValueError) as exc:
            raise NginxSiteConfigError("Internal port must be an integer.") from exc
        if clean_port < 1 or clean_port > 65535:
            raise NginxSiteConfigError("Internal port must be between 1 and 65535.")
        return clean_port

    def _validate_tls_options(self, tls: NginxTlsOptions) -> NginxTlsOptions:
        if tls.enabled and (not tls.certificate_path or not tls.certificate_key_path):
            raise NginxSiteConfigError(
                "TLS-enabled nginx site configs require certificate and key paths."
            )
        return tls
