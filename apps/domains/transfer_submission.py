"""Durable admission at the paid transfer boundary, independent of local completion."""

import hashlib
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import validate_domain_name
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.billing.models import Invoice
from apps.domains.contact_bindings import provider_scope
from apps.domains.contact_validation import validate_order_contacts
from apps.domains.models import ContactVerification, Domain, DomainContact, DomainOrder, DomainTransfer, DomainTransferAttempt
from apps.domains.registrar_policy import ALL_ROLES, policy_for
from apps.domains.registration_submission import _check_bindings, _identifier
from apps.domains.services import DomainContactService


class TransferReviewRequired(ValueError):
    pass


class TransferInProgress(ValueError):
    pass


def _audit(attempt, action):
    AuditLog.objects.create(action="domain.transfer." + action, model_name="DomainTransfer",
                           object_id=str(attempt.transfer_id), data={"transfer_attempt_id": attempt.pk})


def _identity(transfer):
    fields = ("user_id", "invoice_id", "domain_name", "tld", "auto_renew", "dns_provider", "quoted_price", "total_price")
    return {"transfer": {field: str(getattr(transfer, field)) if isinstance(getattr(transfer, field), Decimal)
                         else getattr(transfer, field) for field in fields},
            "roles": {role: getattr(transfer, role + "_id") for role in ALL_ROLES},
            "auth_digest": hashlib.sha256(transfer.auth_code.encode()).hexdigest()}


def _same_transfer(attempt, transfer):
    subject = _identity(transfer)
    return (all(subject[key] == attempt.subject.get(key) for key in subject)
            and transfer.registrar_contact_ids == attempt.role_ids
            and _identifier(transfer.registrar_order_id) == attempt.remote_order_id)


def _locked_subject(transfer_id, customer_id):
    transfer = DomainTransfer.objects.select_for_update().get(pk=transfer_id)
    owner = User.objects.select_for_update().get(pk=transfer.user_id)
    if not owner.is_active or owner.is_staff or owner.is_superuser:
        raise ValidationError("An active customer account is required for transfer.")
    if transfer.status in {"cancelled", "completed"}:
        raise ValidationError("This transfer cannot start another paid submission.")
    if transfer.invoice_id:
        invoice = Invoice.objects.select_for_update().get(pk=transfer.invoice_id)
        if invoice.user_id != owner.pk or invoice.status != "paid":
            raise ValidationError("A paid invoice belonging to this customer is required.")
    elif transfer.status not in {"paid", "processing"}:
        raise ValidationError("A transfer without an invoice must be marked paid before submission.")
    validate_domain_name(transfer.domain_name)
    if (transfer.domain_name != transfer.domain_name.strip().lower().rstrip(".")
            or transfer.tld != transfer.tld.strip().lower().lstrip(".")):
        raise ValidationError("Normalize the domain and extension before transfer.")
    if policy_for(transfer.tld).roles != ALL_ROLES:
        raise ValidationError("This extension requires a registrar-specific transfer workflow. Contact support before submitting a transfer.")
    contacts = {contact.pk: contact for contact in DomainContact.objects.select_for_update().filter(
        pk__in={getattr(transfer, role + "_id") for role in ALL_ROLES}).order_by("pk")}
    for role in ALL_ROLES:
        setattr(transfer, role, contacts[getattr(transfer, role + "_id")])
    validate_order_contacts(transfer, customer_id, transfer.tld)
    if len(transfer.auth_code) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in transfer.auth_code):
        raise ValidationError("The transfer authorization code contains unsupported characters.")
    if transfer.domain_id or Domain.objects.filter(name__iexact=transfer.domain_name).exists():
        raise ValidationError("This domain already has a local record. Reconcile its association before submitting a transfer.")
    if DomainOrder.objects.filter(domain_name__iexact=transfer.domain_name).exists():
        raise ValidationError("A registration request exists for this domain. Review both workflows before submitting a transfer.")
    subject = _identity(transfer)
    subject["contacts"] = {str(pk): ContactVerification.digest_for(contact) for pk, contact in contacts.items()}
    if transfer.invoice_id:
        subject["invoice"] = {"pk": invoice.pk, "user_id": invoice.user_id, "total": str(invoice.total),
                              "amount_paid": str(invoice.amount_paid), "currency": invoice.currency}
    return transfer, contacts, subject


def _claim(transfer_id, customer_id, nameservers, client):
    scope = provider_scope(client)
    with transaction.atomic(durable=True):
        transfer = DomainTransfer.objects.select_for_update().get(pk=transfer_id)
        attempt = DomainTransferAttempt.objects.select_for_update().filter(transfer=transfer).first()
        if attempt:
            if attempt.legacy_review_required or attempt.status in {"submitting", "unknown"}:
                raise TransferReviewRequired("An earlier transfer may have reached the registrar. Owner review is required before continuing.")
            if attempt.status == "preparing":
                raise TransferInProgress("Another worker is preparing this transfer. Do not submit it again.")
            if attempt.status == "accepted":
                if (transfer.status == "cancelled" or not _same_transfer(attempt, transfer)
                        or attempt.provider_scope != scope or attempt.customer_id != customer_id):
                    raise TransferReviewRequired("The acknowledged transfer association changed. Owner review is required.")
                return attempt
        if transfer.registrar_order_id or transfer.domain_id:
            attempt, _ = DomainTransferAttempt.objects.update_or_create(transfer=transfer, defaults={
                "token": uuid.uuid4(), "status": "unknown", "legacy_review_required": True,
                "subject": _identity(transfer), "provider_scope": "", "customer_id": "", "role_ids": {},
                "remote_order_id": "", "submitted_at": None, "completed_at": None,
            })
            _audit(attempt, "historical_review_required")
            return attempt
        transfer, contacts, subject = _locked_subject(transfer_id, customer_id)
        if not 2 <= len(nameservers) <= 13 or len(set(nameservers)) != len(nameservers):
            raise ValidationError("Choose 2 to 13 distinct nameservers before transfer.")
        for name in nameservers:
            validate_domain_name(name)
            if name != name.strip().lower().rstrip("."):
                raise ValidationError("Normalize nameservers before transfer.")
        subject["nameservers"] = list(nameservers)
        if not isinstance(transfer.registrar_contact_ids, dict):
            raise TransferReviewRequired("The retained transfer contact snapshot requires owner review.")
        if transfer.registrar_contact_ids.get("_scope") and transfer.registrar_contact_ids["_scope"] != scope:
            raise TransferReviewRequired("The registrar account differs from the retained transfer contacts.")
        attempt, _ = DomainTransferAttempt.objects.get_or_create(transfer=transfer)
        attempt.token, attempt.status = uuid.uuid4(), "preparing"
        attempt.provider_scope, attempt.customer_id = scope, customer_id
        attempt.subject, attempt.role_ids, attempt.remote_order_id = subject, {}, ""
        attempt.submitted_at = attempt.completed_at = None
        attempt.save()
        _audit(attempt, "preparing")
        return attempt


def submit_transfer(transfer_id, customer_id, nameservers, *, client):
    customer_id = str(customer_id).strip()
    attempt = _claim(transfer_id, customer_id, nameservers, client)
    if attempt.legacy_review_required:
        raise TransferReviewRequired("This historical transfer requires owner review; its stored identifier is not submission proof.")
    if attempt.status == "accepted":
        return attempt.remote_order_id
    token = attempt.token
    try:
        transfer = DomainTransfer.objects.get(pk=transfer_id)
        service, synced = DomainContactService(client), {}
        role_ids = {"_scope": attempt.provider_scope}
        for role in ALL_ROLES:
            contact = getattr(transfer, role)
            if contact.pk not in synced:
                synced[contact.pk] = service.sync_remote_contact(contact, customer_id, transfer.tld)
            role_ids["reg" if role == "registration_contact" else role.removesuffix("_contact")] = synced[contact.pk]
        with transaction.atomic(durable=True):
            current, contacts, subject = _locked_subject(transfer_id, customer_id)
            active = DomainTransferAttempt.objects.select_for_update().get(pk=attempt.pk)
            subject["nameservers"] = list(nameservers)
            if (active.status != "preparing" or active.token != token or subject != attempt.subject
                    or provider_scope(client) != attempt.provider_scope):
                raise ValidationError("Transfer details changed during preparation. Review before retrying.")
            _check_bindings(current, contacts, customer_id, attempt.provider_scope, role_ids)
            active.status, active.role_ids, active.submitted_at = "submitting", role_ids, timezone.now()
            active.save(update_fields=["status", "role_ids", "submitted_at", "updated_at"])
            current.registrar_contact_ids = role_ids
            current.save(update_fields=["registrar_contact_ids", "updated_at"])
            _audit(active, "submitting")
        with transaction.atomic(durable=True):
            current, contacts, subject = _locked_subject(transfer_id, customer_id)
            active = DomainTransferAttempt.objects.select_for_update().get(pk=attempt.pk)
            subject["nameservers"] = list(nameservers)
            if (active.status != "submitting" or active.token != token or subject != attempt.subject
                    or provider_scope(client) != attempt.provider_scope):
                raise TransferReviewRequired("Transfer details changed before provider submission. Owner review is required.")
            _check_bindings(current, contacts, customer_id, attempt.provider_scope, role_ids)
            response = client.transfer_domain(domain_name=current.domain_name, customer_id=customer_id,
                reg_contact_id=role_ids["reg"], admin_contact_id=role_ids["admin"], tech_contact_id=role_ids["tech"],
                billing_contact_id=role_ids["billing"], nameservers=list(nameservers), auth_code=current.auth_code,
                auto_renew=current.auto_renew)
            identifier = ""
            if isinstance(response, dict) and str(response.get("status", "")).lower() != "error" and str(response.get("actionstatus", "")).lower() not in {"error", "failed", "failure"}:
                identifier = _identifier(response.get("entityid") or response.get("orderid") or response.get("order-id"))
            if not identifier:
                raise TransferReviewRequired("The registrar did not acknowledge a valid transfer order. Owner review is required.")
            current.registrar_order_id = identifier
            current.save(update_fields=["registrar_order_id", "updated_at"])
            active.status, active.remote_order_id, active.completed_at = "accepted", identifier, timezone.now()
            active.save(update_fields=["status", "remote_order_id", "completed_at", "updated_at"])
            _audit(active, "acknowledged")
        return identifier
    except BaseException:
        with transaction.atomic(durable=True):
            active = DomainTransferAttempt.objects.select_for_update().get(pk=attempt.pk)
            if active.token == token and active.status in {"preparing", "submitting"}:
                active.status = "aborted" if active.status == "preparing" else "unknown"
                active.completed_at = timezone.now()
                active.save(update_fields=["status", "completed_at", "updated_at"])
                _audit(active, active.status)
        raise
