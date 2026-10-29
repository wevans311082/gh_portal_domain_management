"""Explicit customer acceptance fixture for tests that exercise paid submission."""
import uuid

from django.contrib.auth import get_user_model

from apps.domains.agreements import current_agreement_plan, publish_agreement, record_agreement_acceptance
from apps.domains.models import ContactVerification, DomainTransfer, RegistrarAgreementVersion
from apps.domains.registrar_policy import PROFILE_TLDS


def publish_test_agreement():
    owner, _ = get_user_model().objects.get_or_create(email="paid-agreement-fixture-owner@example.test",
        defaults={"is_active": True, "is_staff": True, "is_superuser": True})
    if not RegistrarAgreementVersion.objects.filter(agreement_key="domain-service", version="paid-fixture-v1").exists():
        publish_agreement(actor=owner, submission_key=uuid.uuid4(), agreement_key="domain-service", version="paid-fixture-v1",
            title="Paid submission test agreement", body="Test fixture only; not real legal terms.",
            profile_keys=list(PROFILE_TLDS),
            operations=["registration", "transfer"], reviewed=True)


def attach_agreement(target):
    target.refresh_from_db()
    publish_test_agreement()
    operation = "transfer" if isinstance(target, DomainTransfer) else "registration"
    plan = current_agreement_plan(target.domain_name, operation)
    contact = target.registration_contact
    receipt = record_agreement_acceptance(actor=target.user, contact_id=contact.pk, domain_name=target.domain_name,
        operation=operation, key=uuid.uuid4(), subject_digest=ContactVerification.digest_for(contact),
        plan_digest=plan.digest, accepted=True)
    target.agreement_acceptance = receipt
    target.save(update_fields=["agreement_acceptance"])
    return target


def accept_cart_item(item):
    from apps.domains.cart_agreements import cart_agreement_target, accept_cart_agreements
    publish_test_agreement()
    target, contact, operation, digest = cart_agreement_target(item.cart.user, item.pk)
    return accept_cart_agreements(customer=item.cart.user, item_id=item.pk, context_digest=digest,
        key=uuid.uuid4(), subject_digest=ContactVerification.digest_for(contact),
        plan_digest=current_agreement_plan(item.domain_name, operation).digest, accepted=True)
