"""Continued registry declarations bound to the current captured association."""

from django.core.exceptions import ValidationError


def validate_declared_renewal(domain, extension):
    from apps.domains.contact_bindings import provider_scope
    from apps.domains.contact_change_confirmation import identifier
    from apps.domains.models import ContactVerification, Domain, DomainContactChange, DomainOrder, DomainTransfer
    from apps.domains.registrar_policy import policy_for, renewal_review_label, tld_for_domain
    label = renewal_review_label(extension)
    if not label or tld_for_domain(domain.name) != extension:
        raise ValidationError("This renewal declaration policy requires review.")
    current = Domain.objects.filter(pk=domain.pk).values("user_id", "name", "registrar_id", "status", "user__is_active").first()
    if (current is None or not current["user__is_active"]
            or any(current[field] != getattr(domain, field) for field in ("user_id", "name", "registrar_id", "status"))
            or current["status"] not in {Domain.STATUS_ACTIVE, Domain.STATUS_EXPIRED, Domain.STATUS_SUSPENDED}):
        raise ValidationError("The domain or customer changed. Review the current registrar association before renewal.")
    from apps.domains.registrar_review import assert_registrar_review_complete
    assert_registrar_review_complete(domain)
    sources = []
    for model, relation, kind in ((DomainOrder, "registration_attempt", "registration"),
                                  (DomainTransfer, "submission_attempt", "transfer")):
        source = model.objects.select_related("registration_contact").filter(domain=domain, user_id=domain.user_id,
            domain_name=domain.name, tld=extension, registrar_order_id=domain.registrar_id).first()
        if source:
            sources.append((source, relation, kind))
    if len(sources) != 1:
        raise ValidationError(f"The {label} registrant association needs review before renewal. Contact support to confirm the current registrant.")
    source, relation, kind = sources[0]
    attempt = getattr(source, relation, None)
    contact = source.registration_contact
    if (source.status != "completed" or contact.user_id != domain.user_id or not identifier(domain.registrar_id)
            or attempt is None or attempt.status != "accepted" or attempt.legacy_review_required
            or attempt.remote_order_id != domain.registrar_id or attempt.provider_scope != provider_scope()
            or not isinstance(source.registrar_contact_ids, dict)
            or source.registrar_contact_ids.get("_scope") != attempt.provider_scope):
        raise ValidationError(f"The current {label} registrant association requires owner review before renewal.")
    for role in policy_for(extension).roles:
        key = "reg" if role == "registration_contact" else role.removesuffix("_contact")
        if not identifier(source.registrar_contact_ids.get(key)):
            raise ValidationError(f"The {label} registrar contact roles require review before renewal.")
    contact.as_resellerclub_payload(attempt.customer_id, extension)
    confirmed = DomainContactChange.objects.filter(domain=domain, user_id=domain.user_id,
        domain_name=domain.name, registrar_order_id=domain.registrar_id, status="confirmed").order_by("-confirmed_at", "-pk").first()
    digest = ContactVerification.digest_for(contact)
    if confirmed:
        matches = (confirmed.subject.get("source_kind") == kind and confirmed.subject.get("source_id") == source.pk
            and confirmed.registration_contact_id == contact.pk and confirmed.role_ids == source.registrar_contact_ids
            and confirmed.subject.get("contact_digests", {}).get("registration_contact") == digest)
    else:
        matches = attempt.subject.get("contacts", {}).get(str(contact.pk)) == digest and attempt.role_ids == source.registrar_contact_ids
    if not matches:
        stage = "registrar confirmation" if confirmed else "registrar submission"
        raise ValidationError(f"The {label} contact declaration changed since {stage}. Confirm the current association before renewal.")
