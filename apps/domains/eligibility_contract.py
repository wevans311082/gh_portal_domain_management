"""Documented eligibility request scopes; observations are not approval mappings.

Contract: https://manage.resellerclub.com/kb/kb/answer/1505
Reviewed 2026-10-28. Legacy response status values are not enumerated there.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError

from apps.domains.contact_change_confirmation import identifier

CRITERIA = {"asia": ("CED_ASIAN_COUNTRY", "CED_DETAILS"), "ca": ("CPR",),
    "coop": ("SPONSORS",), "es": ("ES_CONTACT_IDENTIFICATION_DETAILS",),
    "eu": ("EUROPEAN_COUNTRY",), "ru": ("RU_CONTACT_INFO",), "us": ("APP_PREF_NEXUS",)}


def eligibility_request(contact_ids, tld):
    if not isinstance(tld, str) or tld not in CRITERIA:
        raise ValidationError("This extension has no reviewed registrar eligibility-check contract.")
    if (not isinstance(contact_ids, (list, tuple)) or not 1 <= len(contact_ids) <= 20
            or any(not identifier(value) for value in contact_ids)):
        raise ValidationError("Supply between one and twenty valid registrar contact IDs.")
    ids = [identifier(value) for value in contact_ids]
    if len(set(ids)) != len(ids):
        raise ValidationError("Check each registrar contact once.")
    return {"contact-id": ids, "eligibility-criteria": list(CRITERIA[tld])}


@dataclass(frozen=True)
class EligibilityObservation:
    contact_id: str
    criterion: str
    provider_value: str
    status: str = "unknown"


def eligibility_observations(payload, *, contact_ids, tld):
    """Retain only matched criterion/status tokens, never messages or attributes.

    The adapter deliberately has no success interpretation until a test-account
    contract establishes it. It cannot create a verified ContactVerification.
    """
    request = eligibility_request(contact_ids, tld)
    if not isinstance(payload, dict) or len(payload) != len(request["contact-id"]):
        raise ValidationError("Registrar eligibility response requires contract review.")
    if set(payload) != set(request["contact-id"]):
        raise ValidationError("Registrar eligibility contacts could not be matched.")
    result = []
    for contact_id in request["contact-id"]:
        criteria = payload[contact_id]
        if not isinstance(criteria, dict) or set(criteria) != set(request["eligibility-criteria"]):
            raise ValidationError("Registrar eligibility criteria could not be matched.")
        for criterion in request["eligibility-criteria"]:
            value = criteria[criterion]
            # Only compact status tokens are retained; free text can contain PII.
            import re
            if type(value) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", value):
                raise ValidationError("Registrar eligibility status requires contract review.")
            result.append(EligibilityObservation(contact_id, criterion, value))
    return tuple(result)
