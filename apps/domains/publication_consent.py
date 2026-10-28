"""Organization RDDS publication is opt-in, versioned and separate from validation."""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.domains.models import ContactVerification, DomainContact, RegistrantPublicationDecision
from apps.domains.registrar_policy import tld_for_domain

POLICY_VERSION = "rdds-org-2026-10-28"
REVIEWED_GTLDS = frozenset({"com", "net", "org"})
CHOICES = [("redact", "Keep organization redacted (default)"), ("publish", "Publish organization for this request")]
DISCLOSURE = ("For supported generic domains, an organization name is redacted from public WHOIS/RDAP by default. "
    "Choose Publish only if you are authorized to consent to public disclosure of the registrant organization's name. "
    "This applies to this domain request; it does not publish an individual's contact details or update other existing domains.")


def validate_choice(choice, contact, domain_name):
    if not isinstance(choice, str) or choice not in {"redact", "publish"}:
        raise ValidationError("Choose whether to keep the organization redacted or explicitly publish it.")
    if choice == "publish" and (contact is None or contact.registrant_type != "organisation" or not contact.company.strip()
            or tld_for_domain(domain_name) not in REVIEWED_GTLDS):
        raise ValidationError("Organization publication is available only for an organization contact on a reviewed generic extension (.com, .net or .org).")


def capture_decision(*, customer, contact, domain_name, choice="redact", actor=None):
    """Called inside the request/cart transaction; no inferred affirmative legacy consent."""
    validate_choice(choice, contact, domain_name)
    if choice == "redact":
        return None  # No affirmative publication consent; null is an explicit safe default.
    actor = actor or customer
    if (actor.pk != customer.pk or not customer.is_active or customer.is_staff or customer.is_superuser
            or contact.user_id != customer.pk):
        raise ValidationError("Only the active customer can authorize publication for an owned registrant contact.")
    from apps.audit.models import AuditLog
    with transaction.atomic():
        from apps.accounts.models import User
        current_customer = User.objects.select_for_update().filter(pk=customer.pk, is_active=True, is_staff=False, is_superuser=False).first()
        current_contact = DomainContact.objects.select_for_update().filter(pk=contact.pk, user_id=customer.pk).first()
        if current_customer is None or current_contact is None:
            raise ValidationError("Only the active customer can authorize publication for an owned registrant contact.")
        if ContactVerification.digest_for(current_contact) != ContactVerification.digest_for(contact):
            raise ValidationError("The registrant contact changed while consent was being recorded. Review the current details and choose again.")
        validate_choice(choice, current_contact, domain_name)
        decision = RegistrantPublicationDecision.objects.create(customer=customer, contact=contact,
            domain_name=domain_name.strip().lower().rstrip("."), choice=choice,
            subject_digest=ContactVerification.digest_for(current_contact), policy_version=POLICY_VERSION)
        AuditLog.objects.create(user=actor, action="domain.publication.consent_captured", model_name="RegistrantPublicationDecision",
            object_id=str(decision.pk), data={"policy_version": POLICY_VERSION, "choice": choice})
    return decision


def publication_snapshot(subject, contact, *, customer_id=None):
    """Recheck retained evidence under the paid workflow transaction, not a cached FK."""
    identifier = subject.publication_decision_id
    if identifier is None:
        return {"decision_id": None, "consent": False}
    contact = DomainContact.objects.select_for_update().get(pk=contact.pk)
    decision = RegistrantPublicationDecision.objects.select_for_update().get(pk=identifier)
    if (decision.customer_id != (customer_id if customer_id is not None else subject.user_id) or decision.contact_id != contact.pk
            or decision.domain_name != subject.domain_name):
        raise ValidationError("Organization publication consent is stale or does not match this request.")
    if decision.withdrawn_at:
        return {"decision_id": decision.pk, "consent": False}
    if (decision.choice != "publish" or decision.policy_version != POLICY_VERSION
            or decision.subject_digest != ContactVerification.digest_for(contact)):
        raise ValidationError("Organization publication consent is stale or does not match this request. Choose redaction or confirm the current details before submission.")
    validate_choice("publish", contact, subject.domain_name)
    return {"decision_id": decision.pk, "consent": True, "policy_version": decision.policy_version,
            "subject_digest": decision.subject_digest}


def publication_attributes(domain_name, consent):
    """Only reviewed gTLDs use this attribute; default false never implies opt-in."""
    if type(consent) is not bool:
        raise ValidationError("Organization publication consent must be an explicit boolean.")
    if tld_for_domain(domain_name) not in REVIEWED_GTLDS:
        if consent:
            raise ValidationError("Organization publication is not supported for this extension.")
        return {}
    return {"attr-name1": "registrant-org-consent", "attr-value1": "true" if consent else "false"}


def posted_choice(data):
    if hasattr(data, "getlist") and len(data.getlist("publication_choice")) > 1:
        raise ValidationError("Submit one organization publication choice.")
    return data.get("publication_choice") or "redact"


def withdraw_decision(customer, identifier):
    """Stop future affirmative submission and track already-submitted redaction as support work."""
    from apps.accounts.models import User
    from apps.domains.models import DomainOrder, DomainTransfer, DomainContactChange, PublicationRedaction
    from apps.audit.models import AuditLog
    from django.utils import timezone
    with transaction.atomic():
        actor = User.objects.select_for_update().filter(pk=customer.pk, is_active=True).first()
        if actor is None:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        decision = RegistrantPublicationDecision.objects.select_for_update().get(pk=identifier, customer=actor)
        if decision.withdrawn_at:
            return decision
        work_needed = (DomainOrder.objects.filter(publication_decision=decision).exclude(registrar_order_id="").exists()
            or DomainOrder.objects.filter(publication_decision=decision, registration_attempt__status__in=["submitting", "unknown", "accepted"]).exists()
            or DomainTransfer.objects.filter(publication_decision=decision).exclude(registrar_order_id="").exists()
            or DomainTransfer.objects.filter(publication_decision=decision, submission_attempt__status__in=["submitting", "unknown", "accepted"]).exists()
            or DomainContactChange.objects.filter(publication_decision=decision, submitted_at__isnull=False).exists())
        if work_needed:
            from apps.support.models import SupportTicket, SupportTicketMessage
            ticket = SupportTicket.objects.create(user=actor, subject=f"Organization publication withdrawal: {decision.domain_name}"[:255], priority="high")
            SupportTicketMessage.objects.create(ticket=ticket, user=actor, content=(
                f"Organization publication consent {decision.pk} was withdrawn for {decision.domain_name}. "
                "The order may already have reached the registrar. Review the original association and apply organization redaction at the provider. "
                "This local withdrawal is not confirmation of provider redaction. Do not submit another registration, transfer or contact replacement."))
            decision.withdrawal_ticket = ticket
        decision.withdrawn_at = timezone.now()
        decision.save(update_fields=["withdrawn_at", "withdrawal_ticket", "updated_at"])
        if work_needed:
            redaction, _ = PublicationRedaction.objects.get_or_create(decision=decision)
            from apps.domains.publication_redaction_jobs import publish_redaction
            transaction.on_commit(lambda: publish_redaction(redaction.pk))
        AuditLog.objects.create(user=actor, action="domain.publication.consent_withdrawn", model_name="RegistrantPublicationDecision",
            object_id=str(decision.pk), data={"provider_processing_required": work_needed, "ticket_id": decision.withdrawal_ticket_id})
        return decision
