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
    min_nameservers: int = 2
    eligibility_workstream: str = "general"
    email_workstream: str = "registry_specific"
    service_contact_tld: str | None = None


ALL_ROLES = ("registration_contact", "admin_contact", "tech_contact", "billing_contact")
# Structural namespaces are separate from the enabled/reviewed policy catalogue.
UK_NAMESPACES = frozenset({"co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk", "nic.uk",
    "ac.uk", "gov.uk", "mod.uk", "mil.uk", "nhs.uk", "parliament.uk", "police.uk"})
GENERAL = ContactPolicy("Contact", ALL_ROLES, True, email_workstream="icann_raa")
IO = ContactPolicy("Contact", ALL_ROLES, True)
UK = ContactPolicy("UkContact", ("registration_contact",), False, 10, False, False,
                   eligibility_workstream="uk_accuracy")
US = ContactPolicy("Contact", ALL_ROLES, False, eligibility_workstream="us_nexus",
                   service_contact_tld="com")
EU = ContactPolicy("EuContact", ("registration_contact",), False, max_nameservers=9,
                   lock_supported=False, min_nameservers=1,
                   eligibility_workstream="eu_presence")

# Membership is reviewed explicitly; suffix similarity never grants eligibility.
# EU remains a prepared profile, excluded from ordering until its contract is reviewed.
PROFILE_TLDS = {
    "general_raa": (GENERAL, ("com", "net", "org")),
    "general_registry": (IO, ("io",)),
    "uk_accuracy": (UK, ("uk", "co.uk", "org.uk", "me.uk")),
    "us_nexus": (US, ("us",)),
}


def build_policy_catalogue(profiles):
    catalogue = {}
    for policy, extensions in profiles.values():
        for extension in extensions:
            if not extension or extension != extension.lower().lstrip(".") or extension in catalogue:
                raise ValueError("TLD profile membership must be normalized and unique.")
            catalogue[extension] = policy
    return catalogue


POLICIES = build_policy_catalogue(PROFILE_TLDS)
PREPARED_POLICIES = {"eu": EU}


def email_workstream_tlds(workstream="icann_raa"):
    return tuple(tld for tld, policy in POLICIES.items() if policy.email_workstream == workstream)


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


def contact_payload_tld(tld, role):
    """US nexus belongs to the registrant, not administrative service contacts."""
    if role not in ALL_ROLES:
        raise ValidationError("Choose a recognized registrar contact role.")
    extension = str(tld or "").strip().lower().lstrip(".")
    policy = policy_for(extension)
    return policy.service_contact_tld if role != "registration_contact" and policy.service_contact_tld else extension


def contact_workstream_attributes(contact, tld):
    """Run shared eligibility work; declarations never become identity evidence."""
    # A contact may be edited without ordering; prepared payload contracts do
    # not grant permission to register an extension.
    policy = GENERAL if not tld else PREPARED_POLICIES.get(tld)
    if policy is None:
        policy = policy_for(tld)
    stream = policy.eligibility_workstream
    if stream == "general":
        return {}
    if stream == "uk_accuracy":
        from apps.domains.registry_requirements import validate_uk_registrant
        validate_uk_registrant(contact, tld)
        return {}
    if stream == "us_nexus":
        from apps.domains.us_registry import us_contact_attributes
        return us_contact_attributes(contact)
    if stream == "eu_presence":
        from apps.domains.eu_registry import eu_contact_attributes
        return eu_contact_attributes(contact)
    raise ValidationError("This eligibility workstream requires registrar review.")
