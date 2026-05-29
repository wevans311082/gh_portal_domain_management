"""Provider registry and selection helpers for hosting provisioning."""
from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.provisioning.providers.base import ProvisioningProvider, ProvisioningProviderError
from apps.provisioning.providers.docker_node import DockerNodeProvider
from apps.provisioning.providers.whm import WHMProvider

PROVIDER_WHM = WHMProvider.provider_key
PROVIDER_DOCKER_NODE = DockerNodeProvider.provider_key
DEFAULT_PROVIDER = PROVIDER_WHM

PROVIDER_CHOICES = [
    (PROVIDER_WHM, WHMProvider.display_name),
    (PROVIDER_DOCKER_NODE, DockerNodeProvider.display_name),
]

_PROVIDER_CLASSES: dict[str, type[ProvisioningProvider]] = {
    PROVIDER_WHM: WHMProvider,
    PROVIDER_DOCKER_NODE: DockerNodeProvider,
}


def get_provider(provider_key: str | None = None, config: dict[str, Any] | None = None) -> ProvisioningProvider:
    """Instantiate a provisioning provider from the registry."""
    key = (provider_key or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER
    provider_class = _PROVIDER_CLASSES.get(key)
    if provider_class is None:
        raise ProvisioningProviderError(f"Unknown provisioning provider: {key}")
    return provider_class(config=config)


def get_provider_for_service(service: Any) -> ProvisioningProvider:
    """Resolve provider from service override, package default, then settings."""
    package = getattr(service, "package", None)
    provider_key = (
        getattr(service, "provisioning_provider", "")
        or getattr(package, "provisioning_provider", "")
        or getattr(settings, "PROVISIONING_DEFAULT_PROVIDER", DEFAULT_PROVIDER)
    )
    config: dict[str, Any] = {}
    package_config = getattr(package, "provisioning_config", None)
    service_config = getattr(service, "provisioning_config", None)
    if isinstance(package_config, dict):
        config.update(package_config)
    if isinstance(service_config, dict):
        config.update(service_config)
    return get_provider(provider_key=provider_key, config=config)


__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDER_CHOICES",
    "PROVIDER_DOCKER_NODE",
    "PROVIDER_WHM",
    "DockerNodeProvider",
    "ProvisioningProvider",
    "ProvisioningProviderError",
    "WHMProvider",
    "get_provider",
    "get_provider_for_service",
]
