"""Provisioning provider contract for hosting backends."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProvisioningProviderError(Exception):
    """Raised when a provisioning provider cannot complete an operation."""


class ProvisioningProvider(ABC):
    """Abstract interface implemented by hosting provisioning backends."""

    provider_key: str = "base"
    display_name: str = "Base provider"

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    @abstractmethod
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
        """Create a hosting site/account and return provider response metadata."""

    @abstractmethod
    def suspend_site(self, *, username: str, reason: str = "") -> dict[str, Any]:
        """Suspend an existing hosting site/account."""

    @abstractmethod
    def delete_site(self, *, username: str, keep_dns: bool = False) -> dict[str, Any]:
        """Delete an existing hosting site/account."""

    @abstractmethod
    def create_mail_domain(self, *, username: str, domain: str) -> dict[str, Any]:
        """Ensure the mail domain exists for a hosting account."""

    @abstractmethod
    def create_backup(self, *, username: str) -> dict[str, Any]:
        """Create or schedule a backup for a hosting account."""

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Return provider health details suitable for admin diagnostics."""
