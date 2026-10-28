"""Read documented registrar publication consent without inferring public RDAP state."""

from dataclasses import dataclass

from django.core.exceptions import ValidationError

from apps.domains.contact_change_confirmation import identifier


@dataclass(frozen=True)
class PublicationObservation:
    order_id: str
    applicable: bool
    consent: bool | None


def publication_observation(payload, *, order_id, domain_name, customer_id):
    """Only exact scoped order identity and documented consent values are evidence.

    A missing/null consent is distinct from an explicitly revoked value. The
    result describes provider consent, not public-directory propagation.
    """
    if (not identifier(order_id) or not identifier(customer_id) or not isinstance(payload, dict)
            or identifier(payload.get("orderid", payload.get("entityid"))) != identifier(order_id)
            or any(identifier(payload[key]) != identifier(order_id) for key in ("orderid", "entityid") if key in payload)
            or payload.get("domainname") != domain_name
            or identifier(payload.get("customerid")) != identifier(customer_id)
            or str(payload.get("status", "")).upper() == "ERROR"):
        raise ValidationError("Registrar publication identity could not be confirmed.")
    details = payload.get("registrantOrgConsentDetails")
    if not isinstance(details, dict):
        raise ValidationError("Registrar publication evidence is missing.")
    applicable = details.get("isRegistrantOrgConsentApplicable")
    consent = details.get("registrantOrgConsent")
    if type(applicable) is not str or applicable not in {"true", "false"}:
        raise ValidationError("Registrar publication applicability requires review.")
    if "registrantOrgConsent" not in details or type(consent) not in (str, type(None)) or consent not in {"true", "false", "<null>", None}:
        raise ValidationError("Registrar publication consent requires review.")
    return PublicationObservation(identifier(order_id), applicable == "true",
        True if consent == "true" else False if consent == "false" else None)
