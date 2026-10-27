from django.core.checks import Error, Tags, register


@register(Tags.security)
def check_route_policies(app_configs, **kwargs):
    from .access import CAPABILITIES, ROUTE_POLICIES, SPECIAL_ROUTES
    from .urls import urlpatterns
    names = {pattern.name for pattern in urlpatterns}
    covered = set(ROUTE_POLICIES) | SPECIAL_ROUTES
    errors = []
    if names != covered:
        errors.append(Error(
            "Admin route permission inventory is incomplete or stale.",
            hint=f"Missing policies: {sorted(names - covered)}; unknown routes: {sorted(covered - names)}",
            id="admin_tools.E001",
        ))
    capabilities = {code for code, _ in CAPABILITIES}
    for name, policy in ROUTE_POLICIES.items():
        if any(code and code not in capabilities for code in (policy.read, policy.write, *policy.read_any)):
            errors.append(Error(f"Unknown capability for {name}.", id="admin_tools.E002"))
    return errors


@register(Tags.security, deploy=True)
def check_integration_encryption(app_configs, **kwargs):
    from .secrets import IntegrationSecretError, keyring
    try:
        keyring()
    except IntegrationSecretError as error:
        return [Error(str(error), id="admin_tools.E003",
                      hint="Supply INTEGRATION_ENCRYPTION_KEYS and INTEGRATION_ENCRYPTION_ACTIVE_KEY outside the database.")]
    return []
