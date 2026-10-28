"""Reviewed contact-routing policies; these are not identity verification.

Sources checked 2026-10-28:
https://manage.resellerclub.com/kb/answer/752
https://manage.resellerclub.com/kb/servlet/KBServlet/faq1225.html
https://manage.resellerclub.com/kb/node/3503
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError


@dataclass(frozen=True)
class ContactPolicy:
    contact_type: str
    roles: tuple[str, ...]
    privacy_supported: bool
    max_nameservers: int = 13
    lock_supported: bool = True
    auth_code_supported: bool = True


ALL_ROLES = ("registration_contact", "admin_contact", "tech_contact", "billing_contact")
# Structural namespaces are separate from the enabled/reviewed policy catalogue.
UK_NAMESPACES = frozenset({"co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk", "nic.uk",
    "ac.uk", "gov.uk", "mod.uk", "mil.uk", "nhs.uk", "parliament.uk", "police.uk"})
GENERAL = ContactPolicy("Contact", ALL_ROLES, True)
UK = ContactPolicy("UkContact", ("registration_contact",), False, 10, False, False)
POLICIES = {
    "com": GENERAL, "net": GENERAL, "org": GENERAL, "io": GENERAL,
    "uk": UK, "co.uk": UK, "org.uk": UK, "me.uk": UK,
}


def policy_for(tld):
    extension = str(tld or "").strip().lower().lstrip(".")
    try:
        return POLICIES[extension]
    except KeyError:
        raise ValidationError(
            "This domain extension requires a registrar eligibility review before ordering. "
            "Contact support; no registration has been submitted."
        ) from None


def tld_for_domain(domain_name):
    name = str(domain_name or "").strip().lower().rstrip(".")
    for extension in sorted(set(POLICIES) | UK_NAMESPACES, key=len, reverse=True):
        if name.endswith("." + extension):
            return extension
    # Preserve an unrecognised extension for a helpful review failure.
    return name.partition(".")[2]


def registration_contact_ids(domain_name, reg, admin, tech, billing):
    policy = policy_for(tld_for_domain(domain_name))
    ids = dict(zip(ALL_ROLES, (reg, admin, tech, billing)))
    return tuple(ids[role] if role in policy.roles else "-1" for role in ALL_ROLES)
