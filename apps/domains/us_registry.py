"""Explicit .US declarations; contact format is not proof of nexus."""

from django.core.exceptions import ValidationError

NEXUS_CHOICES = [("", "Not requested"), ("C11", "US citizen (individual)"),
    ("C12", "US permanent resident (individual)"), ("C21", "Organization constituted under US law"),
    ("C31", "Foreign organization with regular lawful US activities - review required"),
    ("C32", "Foreign organization with a US office or facility - review required")]
PURPOSE_CHOICES = [("", "Not requested"), ("P1", "Business for profit"), ("P2", "Non-profit activity"),
    ("P3", "Personal use"), ("P4", "Education"), ("P5", "Government")]
POLICY_VERSION = "us-nexus-2026-10-28"


def validate_us_transfer_code(code):
    from apps.domains.transfer_requirements import validate_transfer_code
    return validate_transfer_code("us", code)


def validate_us_declaration(contact):
    errors = {}
    category = contact.us_nexus_category
    purpose = contact.us_application_purpose
    if category not in {"C11", "C12", "C21", "C31", "C32"}:
        errors["us_nexus_category"] = "Declare the registrant's actual US nexus category. It is not inferred from the address."
    if purpose not in {"P1", "P2", "P3", "P4", "P5"}:
        errors["us_application_purpose"] = "Choose the intended use for this US registrant."
    if contact.us_nexus_certified is not True:
        errors["us_nexus_certified"] = "Confirm the nexus declaration under the US registry requirements."
    if category in {"C11", "C12"} and contact.registrant_type != "individual":
        errors["us_nexus_category"] = "Citizen and permanent-resident categories require an individual registrant."
    if category in {"C21", "C31", "C32"} and contact.registrant_type != "organisation":
        errors["us_nexus_category"] = "This nexus category requires an organization registrant."
    if errors:
        raise ValidationError(errors)


def us_contact_attributes(contact):
    validate_us_declaration(contact)
    category, purpose = contact.us_nexus_category, contact.us_application_purpose
    if category in {"C31", "C32"}:
        raise ValidationError({"us_nexus_category": "Foreign-entity nexus requires review of the registry country declaration before automated submission. Contact support."})
    return {"attr-name1": "purpose", "attr-value1": purpose, "attr-name2": "category", "attr-value2": category}


def validate_us_renewal(domain):
    from apps.domains.registrant_renewal import validate_declared_renewal
    return validate_declared_renewal(domain, "us")
