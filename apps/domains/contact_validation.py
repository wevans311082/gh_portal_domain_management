"""Accurate contact serialization; successful serialization is not verification."""

import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from apps.domains.contact_codes import COUNTRY_CODES, CALLING_CODES


def validate_order_contacts(order, customer_id, tld):
    """Check every contact role before the first external contact mutation."""
    from apps.domains.registrar_policy import policy_for, tld_for_domain, contact_payload_tld
    if tld_for_domain(order.domain_name) != str(tld).strip().lower().lstrip("."):
        raise ValidationError("The domain extension does not match the order's TLD policy.")
    for role in policy_for(tld).roles:
        contact = getattr(order, role)
        if contact.user_id != order.user_id:
            raise ValidationError("Every domain contact must belong to the order's customer.")
        contact.as_resellerclub_payload(customer_id, contact_payload_tld(tld, role))


def resellerclub_contact_payload(contact, customer_id, tld=""):
    errors = {}
    if contact.registrant_type not in {"individual", "organisation"}:
        errors["registrant_type"] = "Confirm whether the registrant is an individual or organisation."
    elif contact.registrant_type == "individual" and (contact.company or contact.company_number):
        errors["registrant_type"] = "Individual contacts must not contain organisation details."
    elif contact.registrant_type == "organisation" and not str(contact.company or "").strip():
        errors["company"] = "Enter the organisation or trading name."

    def field(attribute, maximum, required=True):
        value = str(getattr(contact, attribute, "") or "").strip()
        if required and not value:
            errors[attribute] = "Enter this contact detail before registrar submission."
        elif len(value) > maximum:
            errors[attribute] = f"Use at most {maximum} characters for registrar submission."
        elif any(ord(char) < 32 or ord(char) == 127 for char in value):
            errors[attribute] = "Control characters are not allowed."
        return value

    extension = str(tld or "").strip().lower().lstrip(".")
    from apps.domains.registrar_policy import contact_payload_policy
    policy = contact_payload_policy(extension)
    name = field("name", policy.contact_name_max)
    company = field("company", policy.contact_company_max, required=False)
    email = field("email", 254)
    try:
        validate_email(email)
    except ValidationError:
        errors["email"] = "Enter a valid contact email address."
    country = field("country", 2).upper()
    if country not in COUNTRY_CODES:
        errors["country"] = "Choose an assigned ISO two-letter country code, such as GB or US."
    phone_cc = field("phone_country_code", 3)
    if not re.fullmatch(r"[1-9][0-9]{0,2}", phone_cc, flags=re.ASCII):
        errors["phone_country_code"] = "Enter a country calling code of 1 to 3 digits."
    elif phone_cc not in CALLING_CODES:
        errors["phone_country_code"] = "Enter an assigned international calling code, such as 44 or 1."
    raw_phone = field("phone", 32)
    if not re.fullmatch(r"[0-9 ()-]+", raw_phone, flags=re.ASCII):
        errors["phone"] = "Use digits, spaces, parentheses or hyphens."
    phone = re.sub(r"[ ()-]", "", raw_phone)
    if phone_cc == "44" and phone.startswith("0"):
        phone = phone[1:]
    if not re.fullmatch(r"[0-9]{4,12}", phone, flags=re.ASCII):
        errors["phone"] = "Enter a phone number containing 4 to 12 digits."
    if phone and not phone.strip("0"):
        errors["phone"] = "Enter a real contact phone number."
    identifier = str(customer_id or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,18}", identifier, flags=re.ASCII):
        errors["customer_id"] = "A valid registrar customer ID is required."
    payload = {
        "customer-id": identifier,
        "type": policy.contact_type,
        "name": name,
        "company": company or policy.empty_company_value,
        "email": email,
        "address-line-1": field("address_line1", 64),
        "address-line-2": field("address_line2", 64, required=False),
        "city": field("city", 64),
        "state": field("state", 64),
        "zipcode": field("postcode", policy.contact_postcode_max),
        "country": country,
        "phone-cc": phone_cc,
        "phone": phone,
    }
    if errors:
        raise ValidationError(errors)
    from apps.domains.registrar_policy import contact_workstream_attributes
    payload.update(contact_workstream_attributes(contact, extension))
    return payload
