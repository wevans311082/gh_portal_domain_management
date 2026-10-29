"""Shared transfer admission; contact routing alone never enables transfers.

Provider transfer contract checked 2026-10-29:
https://manage.resellerclub.com/kb/answer/758
"""

from django.core.exceptions import ValidationError

from apps.domains.registrar_policy import ALL_ROLES, policy_for


def transfer_policy_for(extension):
    policy = policy_for(extension)
    if policy.transfer_workstream != "epp" or policy.roles != ALL_ROLES:
        raise ValidationError("This extension requires a registrar-specific transfer workflow. Contact support before submitting a transfer.")
    return policy


def validate_transfer_code(extension, code):
    policy = transfer_policy_for(extension)
    if code == "":
        if policy.transfer_auth_optional:
            return
        raise ValidationError("An authorization code is required for this extension before transfer submission.")
    _validate_code(code, policy.transfer_auth_min, policy.transfer_auth_max, policy.transfer_auth_no_spaces)


def validate_transfer_code_storage(code):
    """Permit incomplete estimates, but never persist malformed/oversized secrets."""
    _validate_code(code, 0, 255, False)


def _validate_code(code, minimum, maximum, no_spaces):
    if (not isinstance(code, str) or not minimum <= len(code) <= maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in code)
            or no_spaces and any(char.isspace() for char in code)):
        raise ValidationError(
            f"Transfers for this extension require an authorization code of {minimum}-{maximum} characters"
            + (", without spaces or control characters." if no_spaces else ", without control characters.")
        )


def validate_transfer_nameservers(domain_name, nameservers):
    from django.core.validators import validate_domain_name
    from apps.domains.registrar_policy import tld_for_domain
    from apps.domains.registry_requirements import validate_nameservers
    if not isinstance(domain_name, str) or domain_name != domain_name.strip().lower().rstrip("."):
        raise ValidationError("Normalize the domain name before transfer.")
    validate_domain_name(domain_name)
    transfer_policy_for(tld_for_domain(domain_name))
    validate_nameservers(domain_name, nameservers)
    if any(name != name.strip().lower().rstrip(".") for name in nameservers):
        raise ValidationError("Normalize nameservers before transfer.")
