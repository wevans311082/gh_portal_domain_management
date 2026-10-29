"""Bounded, read-only validation labels; no evidence references or documents."""

from apps.domains.models import ContactVerification, DomainOrder
from apps.domains.registrar_policy import tld_for_domain


def domain_workstream_summary(domain):
    """Describe applicable steps, never their completion or eligibility approval."""
    from django.core.exceptions import ValidationError
    from apps.domains.registrar_policy import policy_for, registry_exception_for, workstream_tlds
    extension = tld_for_domain(domain.name)
    try:
        policy = policy_for(extension)
    except ValidationError:
        return {"reviewed": False, "requirements": "Registrar policy review required"}
    requirements = {"general": "Standard registrant contact details", "uk_accuracy": "UK registrant contact requirements",
        "us_nexus": "US nexus declaration", "eu_presence": "EU eligibility declaration"}.get(policy.eligibility_workstream)
    if requirements is None:
        return {"reviewed": False, "requirements": "Registrar policy review required"}
    members = sorted(workstream_tlds("eligibility", policy.eligibility_workstream))
    return {"reviewed": True, "requirements": requirements,
        "shared_tlds": ", ".join("." + member for member in members[:20]), "members_truncated": len(members) > 20,
        "individual_required": registry_exception_for(extension).natural_person_required,
        "email_process": "ICANN RAA email verification" if policy.email_workstream == "icann_raa"
            else "Registry-specific process; ICANN RAA workflow does not apply",
        "contact_changes": "IRTP authorization checks apply" if policy.contact_change_workstream == "icann_irtp"
            else "Registry-specific contact-change rules apply",
        "incoming_transfer": ("Authorization-code transfer; the code may be supplied during registrar approval"
            if policy.transfer_auth_optional else "Authorization-code transfer; the code is required before submission")
            if policy.transfer_workstream == "epp" else "Registrar-specific transfer review required",
        "publication": "Explicit consent is required to publish the organisation name"
            if policy.publication_workstream == "organisation_consent" else "Registry-specific publication rules apply",
        "privacy": "Privacy may be requested, subject to provider availability" if policy.privacy_supported
            else "Optional privacy service is not supported"}


def evidence_label(evidence):
    if evidence is None:
        return "Not confirmed"
    return evidence.get_status_display() if evidence.is_current else "Needs recheck"


def contact_validation_summary(contact):
    identity = ContactVerification.objects.filter(contact=contact, check_kind="identity").first()
    business = ContactVerification.objects.filter(contact=contact, check_kind="business").first()
    scopes = []
    seen = set()
    # Scope diversity is bounded; this is explicitly a recent-evidence summary.
    for evidence in ContactVerification.objects.filter(contact=contact, check_kind="eligibility")[:25]:
        scope = (evidence.tld, evidence.domain_name)
        if scope in seen:
            continue
        seen.add(scope)
        scopes.append({"tld": evidence.tld or "Scope not specified", "domain": evidence.domain_name,
            "label": evidence_label(evidence) if evidence.tld else "Scope requires review"})
        if len(scopes) == 4:
            break
    return {"identity": evidence_label(identity),
        "business": "Not applicable" if contact.registrant_type == "individual" else evidence_label(business),
        "eligibility": scopes[:3], "eligibility_truncated": len(scopes) > 3}


def domain_eligibility_summary(domain):
    """Contact-level or another domain's approval cannot verify this association."""
    from apps.domains.registrar_review import registrar_review_required
    from apps.domains.contact_change_confirmation import identifier
    if registrar_review_required(domain):
        return "Registrar association review required"
    order = DomainOrder.objects.filter(domain=domain, user_id=domain.user_id, domain_name=domain.name,
        registrar_order_id=domain.registrar_id, registration_contact__user_id=domain.user_id,
        tld=tld_for_domain(domain.name)).first()
    if order is None or not isinstance(order.registrar_contact_ids, dict) or not order.registrar_contact_ids.get("_scope"):
        return "No independent eligibility result recorded"
    if not identifier(domain.registrar_id) or not identifier(order.remote_registrant_id):
        return "Registrar association review required"
    evidence = ContactVerification.objects.filter(contact_id=order.registration_contact_id, domain_order=order,
        check_kind="eligibility", domain_name=domain.name, tld=tld_for_domain(domain.name),
        reference=domain.registrar_id, remote_contact_id=order.remote_registrant_id,
        provider_scope=order.registrar_contact_ids["_scope"]).first()
    return evidence_label(evidence) if evidence else "No independent eligibility result recorded"
