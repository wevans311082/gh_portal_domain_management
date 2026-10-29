"""Agreement review for an owned direct request before registrar admission."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils.crypto import salted_hmac

from apps.audit.models import AuditLog
from .agreements import _digest, assert_acceptance_current, record_agreement_acceptance
from .models import DomainContact, DomainOrder, DomainRegistrationAttempt, DomainTransfer, DomainTransferAttempt
from .registrar_policy import tld_for_domain

REQUESTS = {"registration": (DomainOrder, DomainRegistrationAttempt, "order"),
            "transfer": (DomainTransfer, DomainTransferAttempt, "transfer")}


def request_agreement_target(customer, request_id, *, kind, lock=False):
    if kind not in REQUESTS:
        raise Http404
    model, attempt_model, relation = REQUESTS[kind]
    rows = model.objects.filter(pk=request_id, user_id=customer.pk)
    if lock:
        rows = rows.select_for_update()
    target = rows.first()
    if target is None:
        raise Http404
    attempts = attempt_model.objects.filter(**{relation: target})
    if lock:
        attempts = attempts.select_for_update()
    attempt = attempts.first()
    if (target.domain_id or target.registrar_order_id or target.completed_at
            or target.status not in {"draft", "pending_payment", "paid", "processing", "failed"}
            or (attempt and (attempt.status != "aborted" or attempt.submitted_at
                             or attempt.legacy_review_required or attempt.remote_order_id))):
        raise ValidationError("This request is closed, already admitted or needs registrar review. Its agreement history cannot be replaced here. Contact support.")
    if target.invoice_id and target.invoice.user_id != customer.pk:
        raise ValidationError("The invoice association needs support review before accepting agreements.")
    if target.domain_name != target.domain_name.strip().lower().rstrip(".") or target.tld != tld_for_domain(target.domain_name):
        raise ValidationError("The domain and extension need support review before accepting agreements.")
    contact = DomainContact.objects.filter(pk=target.registration_contact_id, user_id=customer.pk).first()
    if contact is None:
        raise ValidationError("An owned registrant contact is required. Contact support to correct this request.")
    values = {field: str(getattr(target, field)) for field in (
        "user_id", "invoice_id", "domain_name", "tld", "quoted_price", "total_price", "auto_renew", "dns_provider",
        "publication_decision_id", "registration_contact_id", "admin_contact_id", "tech_contact_id", "billing_contact_id")}
    if kind == "registration":
        values.update(years=target.registration_years, privacy=target.privacy_enabled)
    else:
        values["auth_digest"] = _digest(target.auth_code)
    values.update(kind=kind, request=target.pk)
    digest = salted_hmac("direct-agreement-context-v1", _digest(values), algorithm="sha256").hexdigest()
    return target, contact, kind, digest


@transaction.atomic
def accept_request_agreements(*, customer, request_id, kind, context_digest, key, subject_digest, plan_digest, accepted):
    if not customer.is_active or customer.is_staff or customer.is_superuser:
        raise PermissionDenied
    target, contact, operation, digest = request_agreement_target(customer, request_id, kind=kind, lock=True)
    if digest != context_digest:
        raise ValidationError("The request changed. Read its current details and agreements again.")
    receipt = record_agreement_acceptance(actor=customer, contact_id=contact.pk, domain_name=target.domain_name,
        operation=operation, key=key, subject_digest=subject_digest, plan_digest=plan_digest, accepted=accepted)
    assert_acceptance_current(receipt_id=receipt.pk, customer_id=customer.pk, contact_id=contact.pk,
        domain_name=target.domain_name, operation=operation)
    if target.agreement_acceptance_id != receipt.pk:
        target.agreement_acceptance = receipt
        target.save(update_fields=["agreement_acceptance", "updated_at"])
        AuditLog.objects.create(user=customer, action="domain.agreement.attached", model_name=type(target).__name__,
            object_id=str(target.pk), data={"acceptance_id": receipt.pk, "operation": operation})
    return receipt
