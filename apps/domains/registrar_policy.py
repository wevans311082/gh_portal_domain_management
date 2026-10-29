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
    renewal_review_label: str = ""
    publication_workstream: str = "registry_specific"
    contact_change_workstream: str = "registry_specific"
    contact_name_max: int = 255
    contact_company_max: int = 255
    contact_postcode_max: int = 10
    empty_company_value: str = "N/A"
    transfer_workstream: str = "review"
    transfer_auth_min: int = 0
    transfer_auth_max: int = 255
    transfer_auth_no_spaces: bool = False
    transfer_auth_optional: bool = True
    agreement_keys: tuple[str, ...] = ("domain-service",)


@dataclass(frozen=True)
class RegistryException:
    natural_person_required: bool = False
    renewal_review_label: str = ""


ALL_ROLES = ("registration_contact", "admin_contact", "tech_contact", "billing_contact")
# Structural namespaces are separate from the enabled/reviewed policy catalogue.
UK_NAMESPACES = frozenset({"co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk", "nic.uk",
    "ac.uk", "gov.uk", "mod.uk", "mil.uk", "nhs.uk", "parliament.uk", "police.uk"})
GENERAL = ContactPolicy("Contact", ALL_ROLES, True, email_workstream="icann_raa",
                        publication_workstream="organisation_consent", contact_change_workstream="icann_irtp",
                        transfer_workstream="epp")
IO = ContactPolicy("Contact", ALL_ROLES, True, transfer_workstream="epp")
UK = ContactPolicy("UkContact", ("registration_contact",), False, 10, False, False,
                   eligibility_workstream="uk_accuracy")
US = ContactPolicy("Contact", ALL_ROLES, False, eligibility_workstream="us_nexus",
                   service_contact_tld="com", renewal_review_label="US nexus", transfer_workstream="epp",
                   transfer_auth_min=6, transfer_auth_max=16, transfer_auth_no_spaces=True)
EU = ContactPolicy("EuContact", ("registration_contact",), False, max_nameservers=9,
                   lock_supported=False, min_nameservers=1,
                   eligibility_workstream="eu_presence", renewal_review_label="EU eligibility",
                   contact_name_max=50, contact_company_max=100, contact_postcode_max=16,
                   empty_company_value="NA")

# Exceptions extend a shared profile; they do not duplicate its workstreams.
REGISTRY_EXCEPTIONS = {"me.uk": RegistryException(natural_person_required=True,
                                                  renewal_review_label=".me.uk")}


def registry_exception_for(tld):
    return REGISTRY_EXCEPTIONS.get(str(tld or "").strip().lower().lstrip("."), RegistryException())


def renewal_review_label(tld):
    policy = policy_for(tld)
    return registry_exception_for(tld).renewal_review_label or policy.renewal_review_label

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


def profile_key_for(tld):
    extension = str(tld or "").strip().lower().lstrip(".")
    policy_for(extension)
    for key, (policy, members) in PROFILE_TLDS.items():
        if extension in members:
            return key
    raise ValidationError("This extension requires a reviewed shared profile assignment.")


def email_workstream_tlds(workstream="icann_raa"):
    return workstream_tlds("email", workstream)


def workstream_tlds(kind, workstream):
    if kind not in {"eligibility", "email", "publication", "contact_change", "transfer"}:
        raise ValueError("Choose a recognized registrar workstream.")
    return tuple(tld for tld, policy in POLICIES.items() if getattr(policy, kind + "_workstream") == workstream)


def policy_for(tld):
    extension = str(tld or "").strip().lower().lstrip(".")
    try:
        return POLICIES[extension]
    except KeyError:
        raise ValidationError(
            "This domain extension requires a registrar eligibility review before ordering. "
            "Contact support; no registration has been submitted."
        ) from None


def contact_payload_policy(tld):
    """Prepared serialization supports editing; only policy_for enables ordering."""
    extension = str(tld or "").strip().lower().lstrip(".")
    if not extension:
        return GENERAL
    if extension in POLICIES:
        return POLICIES[extension]
    if extension in PREPARED_POLICIES:
        return PREPARED_POLICIES[extension]
    return policy_for(extension)


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
    extension = str(tld or "").strip().lower().lstrip(".")
    policy = contact_payload_policy(extension)
    stream = policy.eligibility_workstream
    if stream == "general":
        return {}
    if stream == "uk_accuracy":
        from apps.domains.registry_requirements import validate_uk_registrant
        validate_uk_registrant(contact, extension)
        return {}
    if stream == "us_nexus":
        from apps.domains.us_registry import us_contact_attributes
        return us_contact_attributes(contact)
    if stream == "eu_presence":
        from apps.domains.eu_registry import eu_contact_attributes
        return eu_contact_attributes(contact)
    raise ValidationError("This eligibility workstream requires registrar review.")
