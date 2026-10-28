"""Reviewed local registry checks; none of these establish external verification.

Current legacy UK rules:
https://registrars.nominet.uk/uk-namespace/registration-and-domain-management/field-definitions-and-registrant-types/
https://www.nominet.uk/wp-content/uploads/2015/10/Rules_June_2014.pdf
Provider operation restrictions:
https://manage.resellerclub.com/kb/servlet/KBServlet/faq1225.html
"""

import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_domain_name

from apps.domains.registrar_policy import UK_NAMESPACES, policy_for, tld_for_domain

UK_EXTENSIONS = frozenset({"uk", "co.uk", "org.uk", "me.uk"})
POLICY_VERSION = "reviewed-2026-10-28"


def validate_uk_registrant(contact, tld):
    if tld not in UK_EXTENSIONS:
        return
    identity = (contact.name if contact.registrant_type == "individual" else contact.company).strip()
    if len(identity) < 4 or sum(character.isalpha() for character in identity) < 3:
        field = "name" if contact.registrant_type == "individual" else "company"
        raise ValidationError({field: "UK registrant names must contain at least four characters, including at least three letters. Supply the actual full person or organization name."})
    street = contact.address_line1.strip()
    if street and all(character.isdecimal() or character.isspace() for character in street):
        raise ValidationError({"address_line1": "UK registry contact data needs non-numeric street information, not only a house number. Supply the actual address."})
    if tld == "me.uk" and contact.registrant_type != "individual":
        raise ValidationError({"registrant_type": ".me.uk is a personal namespace and requires an individual registrant. Organizations and exceptional court/dispute cases need registrar review; choose a suitable extension or an accurate individual contact."})


def validate_registration(domain_name, contact=None, years=1):
    validate_domain_name(domain_name)
    if domain_name in UK_NAMESPACES:
        raise ValidationError("This name is a UK registry namespace, not a registrable customer domain.")
    extension = tld_for_domain(domain_name)
    policy_for(extension)
    if type(years) is not int or not 1 <= years <= 10:
        raise ValidationError("Choose a registration term from one to ten years.")
    label = domain_name[:-(len(extension) + 1)]
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, flags=re.ASCII):
        raise ValidationError("Choose a single valid ASCII domain label of 1 to 63 characters before the extension.")
    if extension in UK_EXTENSIONS:
        if len(domain_name) > 64:
            raise ValidationError("UK names exceeding the registrar's documented 64-character total limit need support review before ordering. This does not invalidate existing registrations.")
        reserved = {"ac", "co", "gov", "ltd", "me", "mil", "mod", "net", "nhs", "nic", "org", "plc", "police", "sch", "com", "uk"}
        if (extension != "uk" and label in reserved) or extension == "uk" and label in {"com", "uk"}:
            raise ValidationError("This UK label is reserved by registry naming rules. Choose another domain or ask support to review an existing historical registration.")
        if len(label) >= 4 and label[2:4] == "--":
            raise ValidationError("UK domains do not support IDN labels or simultaneous hyphens in the third and fourth positions.")
        if contact is not None:
            validate_uk_registrant(contact, extension)
    elif label.startswith("xn--"):
        raise ValidationError("IDN registration requires a reviewed language-specific workflow. Contact support before ordering.")


def capabilities(domain_name):
    try:
        policy = policy_for(tld_for_domain(domain_name))
    except ValidationError:
        return {"reviewed": False, "lock": False, "auth_code": False, "max_nameservers": None, "uk_domain": False}
    return {"reviewed": True, "lock": policy.lock_supported, "auth_code": policy.auth_code_supported,
            "max_nameservers": policy.max_nameservers, "uk_domain": tld_for_domain(domain_name) in UK_EXTENSIONS}


def policy_snapshot(domain_name):
    extension = tld_for_domain(domain_name)
    policy = policy_for(extension)
    return {"version": POLICY_VERSION, "extension": extension, "contact_type": policy.contact_type,
        "natural_person_required": extension == "me.uk", "max_nameservers": policy.max_nameservers,
        "scope": "local_requirements_only"}


def validate_nameservers(domain_name, nameservers):
    policy = policy_for(tld_for_domain(domain_name))
    if not isinstance(nameservers, (list, tuple)) or not 2 <= len(nameservers) <= policy.max_nameservers:
        raise ValidationError(f"Choose between two and {policy.max_nameservers} nameservers for this extension.")
    normalized = []
    for name in nameservers:
        if not isinstance(name, str):
            raise ValidationError("Enter valid nameserver hostnames.")
        validate_domain_name(name)
        normalized.append(name.strip().lower().rstrip("."))
    if len(set(normalized)) != len(normalized):
        raise ValidationError("Choose distinct nameserver hostnames.")


def validate_renewal_registrant(domain):
    """Exceptional .me.uk legacy ownership must be reviewed, not silently renewed."""
    if tld_for_domain(domain.name) != "me.uk":
        return
    from apps.domains.models import DomainOrder
    order = DomainOrder.objects.select_related("registration_contact").filter(domain=domain, user_id=domain.user_id,
        domain_name=domain.name, registrar_order_id=domain.registrar_id).first()
    if order is None or order.registration_contact.user_id != domain.user_id:
        raise ValidationError("The .me.uk registrant association needs review before renewal. Contact support to confirm the actual individual or a documented registry exception.")
    validate_uk_registrant(order.registration_contact, "me.uk")
    attempt = getattr(order, "registration_attempt", None)
    if attempt and attempt.subject.get("contacts"):
        from apps.domains.models import ContactVerification
        digest = attempt.subject["contacts"].get(str(order.registration_contact_id))
        if digest != ContactVerification.digest_for(order.registration_contact):
            raise ValidationError("The .me.uk contact changed since registration. Contact support to confirm current registrar eligibility before renewal.")


def operation_response(request, domain, operation):
    from django.shortcuts import render
    capability = capabilities(domain.name)
    if capability.get(operation):
        return None
    if tld_for_domain(domain.name) in UK_EXTENSIONS:
        message = ("UK domains do not support this registrar lock operation. A saved local lock flag does not confirm a registry lock."
            if operation == "lock" else "UK domains do not use EPP authorization codes. Contact support for the UK registrar transfer process.")
    else:
        message = "This extension requires an operation policy review. Contact support before requesting this registrar operation."
    response = render(request, "domains/unsupported_operation.html", {"domain": domain, "operation_message": message}, status=409)
    response["Cache-Control"] = "no-store"
    return response


def renewal_review_response(request, domain):
    from django.shortcuts import render
    try:
        validate_renewal_registrant(domain)
    except ValidationError as exc:
        response = render(request, "domains/registry_review_required.html", {"domain": domain, "review_message": " ".join(exc.messages)}, status=409)
        response["Cache-Control"] = "no-store"
        return response
    return None


def renewal_review_message(domain):
    try:
        validate_renewal_registrant(domain)
    except ValidationError as exc:
        return " ".join(exc.messages)
    return ""
