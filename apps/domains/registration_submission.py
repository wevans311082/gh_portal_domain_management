"""Durable admission and version checks at the paid registration boundary."""

import re
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import validate_domain_name
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.billing.models import Invoice
from apps.domains.contact_bindings import _fingerprint, provider_scope
from apps.domains.contact_validation import validate_order_contacts
from apps.domains.models import ContactVerification, Domain, DomainContact, DomainOrder, DomainRegistrationAttempt, DomainTransfer, RegistrarContactBinding
from apps.domains.registrar_policy import policy_for
from apps.domains.services import DomainContactService


class RegistrationReviewRequired(ValueError):
    pass


class RegistrationInProgress(ValueError):
    pass


def _identifier(value):
    value = str(value).strip() if type(value) in (str, int) else ""
    return value if re.fullmatch(r"[1-9][0-9]{0,18}", value) else ""


def _audit(attempt, action):
    AuditLog.objects.create(action=action, model_name="DomainOrder", object_id=str(attempt.order_id), data={"registration_attempt_id": attempt.pk})


def _locked_subject(order_id, customer_id, *, allow_existing=False):
    order = DomainOrder.objects.select_for_update().get(pk=order_id)
    owner = User.objects.select_for_update().get(pk=order.user_id)
    if not owner.is_active or owner.is_staff or owner.is_superuser:
        raise ValidationError("An active customer account is required for registration.")
    if order.status in {"paused", "cancelled"}:
        raise ValidationError("The order was paused or cancelled before submission.")
    if order.invoice_id:
        invoice = Invoice.objects.select_for_update().get(pk=order.invoice_id)
        if invoice.user_id != order.user_id or invoice.status != "paid":
            raise ValidationError("A paid invoice belonging to this customer is required.")
    elif order.status not in {"paid", "processing", "completed"}:
        raise ValidationError("The manual registration must be marked paid before submission.")
    validate_domain_name(order.domain_name)
    if order.domain_name != order.domain_name.strip().lower().rstrip(".") or order.tld != order.tld.strip().lower().lstrip("."):
        raise ValidationError("Normalize the domain and extension before registration.")
    policy = policy_for(order.tld)
    ids = {getattr(order, role + "_id") for role in policy.roles}
    contacts = {contact.pk: contact for contact in DomainContact.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
    for role in policy.roles:
        setattr(order, role, contacts[getattr(order, role + "_id")])
    validate_order_contacts(order, customer_id, order.tld)
    if not policy.privacy_supported and order.privacy_enabled:
        order.privacy_enabled = False
        order.save(update_fields=["privacy_enabled", "updated_at"])
    if not allow_existing and Domain.objects.filter(name__iexact=order.domain_name).exists():
        raise ValidationError("This domain already has a local record. Reconcile it before registering again.")
    if not allow_existing and DomainTransfer.objects.filter(domain_name__iexact=order.domain_name).exists():
        raise ValidationError("A transfer request exists for this domain. Review both workflows before registering again.")
    fields = ("user_id", "invoice_id", "domain_name", "tld", "registration_years", "privacy_enabled", "auto_renew", "dns_provider", "quoted_price", "total_price")
    subject = {"order": {field: str(getattr(order, field)) if isinstance(getattr(order, field), Decimal) else getattr(order, field) for field in fields},
               "roles": {role: getattr(order, role + "_id") for role in policy.roles},
               "contacts": {str(pk): ContactVerification.digest_for(contact) for pk, contact in contacts.items()}}
    if order.invoice_id:
        subject["invoice"] = {"pk": invoice.pk, "user_id": invoice.user_id, "total": str(invoice.total),
                              "amount_paid": str(invoice.amount_paid), "currency": invoice.currency}
    return order, contacts, subject


def _same_order(attempt, order):
    return (
        all((str(getattr(order, field)) if isinstance(getattr(order, field), Decimal) else getattr(order, field)) == value for field, value in attempt.subject["order"].items())
        and all(getattr(order, role + "_id") == identifier for role, identifier in attempt.subject["roles"].items())
        and (not attempt.role_ids or order.registrar_contact_ids == attempt.role_ids)
    )


def _claim(order_id, customer_id, nameservers, client):
    scope = provider_scope(client)
    with transaction.atomic(durable=True):
        order = DomainOrder.objects.select_for_update().get(pk=order_id)
        attempt = DomainRegistrationAttempt.objects.select_for_update().filter(order=order).first()
        if attempt:
            if attempt.legacy_review_required:
                raise RegistrationReviewRequired("This historical order has no durable submission evidence. An owner must review its registrar history before processing.")
            if attempt.status in {"submitting", "unknown"}:
                raise RegistrationReviewRequired("An earlier registration may have reached the registrar. Reconcile it before retrying.")
            if attempt.status == "preparing":
                raise RegistrationInProgress("Another worker is preparing this registration. Do not submit it again.")
            if attempt.status == "accepted":
                if not _same_order(attempt, order) or attempt.provider_scope != scope or attempt.customer_id != customer_id or _identifier(order.registrar_order_id) != attempt.remote_order_id:
                    raise RegistrationReviewRequired("The acknowledged registration linkage changed. Reconciliation is required.")
                return attempt, order
        # A pasted or legacy ID is not provider proof. Retain a review marker
        # without syncing contacts or blindly assigning a local domain to it.
        if order.registrar_order_id:
            from apps.domains.historical_registration import historical_subject
            attempt, _ = DomainRegistrationAttempt.objects.update_or_create(order=order, defaults={
                "token": uuid.uuid4(), "status": "unknown", "legacy_review_required": True,
                "provider_scope": "", "customer_id": "", "subject": historical_subject(order), "role_ids": {},
                "remote_order_id": "", "submitted_at": None, "completed_at": None,
            })
            _audit(attempt, "domain.registration.historical_review_required")
            return attempt, order
        if order.registrar_contact_ids.get("_scope") and order.registrar_contact_ids["_scope"] != scope:
            raise RegistrationReviewRequired("The registrar environment changed. Reconcile the existing contact snapshot before retrying.")
        order, contacts, subject = _locked_subject(order_id, customer_id, allow_existing=bool(order.registrar_order_id))
        subject["nameservers"] = list(nameservers)
        if not 2 <= len(nameservers) <= 13:
            raise ValidationError("Supply between two and thirteen nameservers before registration.")
        if order.tld in {"uk", "co.uk", "org.uk", "me.uk"} and len(nameservers) > 10:
            raise ValidationError("UK registrations support at most ten nameservers.")
        if attempt is None:
            attempt = DomainRegistrationAttempt(order=order)
        attempt.token = uuid.uuid4()
        attempt.status = "preparing"
        attempt.provider_scope = scope
        attempt.customer_id = customer_id
        attempt.subject = subject
        attempt.role_ids = {}
        attempt.remote_order_id = ""
        attempt.submitted_at = attempt.completed_at = None
        attempt.save()
        _audit(attempt, "domain.registration.preparing")
        return attempt, order


def _check_bindings(order, contacts, customer_id, scope, role_ids):
    policy = policy_for(order.tld)
    for role in policy.roles:
        contact = contacts[getattr(order, role + "_id")]
        digest = _fingerprint(contact.as_resellerclub_payload(customer_id, order.tld))
        key = "reg" if role == "registration_contact" else role.removesuffix("_contact")
        if not RegistrarContactBinding.objects.filter(
            contact=contact, provider_scope=scope, customer_id=customer_id, contact_type=policy.contact_type,
            payload_digest=digest, remote_id=role_ids[key], status="ready",
        ).exists():
            raise ValidationError("A contact changed after registrar synchronization. Review and retry before paid submission.")


def submit_registration(order_id, customer_id, nameservers, *, client):
    customer_id = str(customer_id).strip()
    attempt, order = _claim(order_id, customer_id, nameservers, client)
    if attempt.legacy_review_required:
        raise RegistrationReviewRequired("The existing registrar identifier requires a read-only historical review before processing.")
    if attempt.status == "accepted":
        return attempt.remote_order_id
    token = attempt.token
    try:
        service = DomainContactService(client)
        role_ids = {"_scope": attempt.provider_scope, "admin": "-1", "tech": "-1", "billing": "-1"}
        synced = {}
        for role in policy_for(order.tld).roles:
            contact = getattr(order, role)
            if contact.pk not in synced:
                synced[contact.pk] = service.sync_remote_contact(contact, customer_id, order.tld)
            key = "reg" if role == "registration_contact" else role.removesuffix("_contact")
            role_ids[key] = synced[contact.pk]
        # Commit the paid-call marker before entering the transaction that holds
        # local subjects stable through the bounded provider request.
        with transaction.atomic(durable=True):
            current, contacts, subject = _locked_subject(order_id, customer_id)
            active = DomainRegistrationAttempt.objects.select_for_update().get(pk=attempt.pk)
            subject["nameservers"] = list(nameservers)
            if active.status != "preparing" or active.token != token or subject != attempt.subject or provider_scope(client) != attempt.provider_scope:
                raise ValidationError("The registration details changed during preparation. Review before retrying.")
            _check_bindings(current, contacts, customer_id, attempt.provider_scope, role_ids)
            active.status = "submitting"
            active.role_ids = role_ids
            active.submitted_at = timezone.now()
            active.save(update_fields=["status", "role_ids", "submitted_at", "updated_at"])
            current.registrar_contact_ids = role_ids
            current.save(update_fields=["registrar_contact_ids", "updated_at"])
            _audit(active, "domain.registration.submitting")
        with transaction.atomic(durable=True):
            current, contacts, subject = _locked_subject(order_id, customer_id)
            active = DomainRegistrationAttempt.objects.select_for_update().get(pk=attempt.pk)
            subject["nameservers"] = list(nameservers)
            if active.status != "submitting" or active.token != token or subject != attempt.subject:
                raise ValidationError("The registration details changed before provider submission. Registrar review is required.")
            _check_bindings(current, contacts, customer_id, attempt.provider_scope, role_ids)
            response = client.register_domain(
                domain_name=current.domain_name, years=current.registration_years, customer_id=customer_id,
                reg_contact_id=role_ids["reg"], admin_contact_id=role_ids["admin"], tech_contact_id=role_ids["tech"],
                billing_contact_id=role_ids["billing"], nameservers=list(nameservers),
                purchase_privacy=current.privacy_enabled, auto_renew=current.auto_renew,
            )
            if isinstance(response, dict):
                identifier = _identifier(response.get("entityid") or response.get("orderid") or response.get("order-id"))
                if str(response.get("status", "")).lower() == "error" or str(response.get("actionstatus", "")).lower() in {"error", "failed", "failure"}:
                    identifier = ""
            else:
                identifier = _identifier(response)
            if not identifier:
                raise RegistrationReviewRequired("The registrar did not acknowledge a valid order identifier. Reconciliation is required.")
            current.registrar_order_id = identifier
            current.save(update_fields=["registrar_order_id", "updated_at"])
            active.remote_order_id = identifier
            active.status = "accepted"
            active.completed_at = timezone.now()
            active.save(update_fields=["remote_order_id", "status", "completed_at", "updated_at"])
            _audit(active, "domain.registration.acknowledged")
        return identifier
    except BaseException:
        # Do not erase a committed uncertain marker when the network or local
        # acknowledgement transaction fails. These writes contain no exception.
        with transaction.atomic(durable=True):
            active = DomainRegistrationAttempt.objects.select_for_update().get(pk=attempt.pk)
            if active.token == token and active.status in {"preparing", "submitting"}:
                active.status = "aborted" if active.status == "preparing" else "unknown"
                active.completed_at = timezone.now()
                active.save(update_fields=["status", "completed_at", "updated_at"])
                _audit(active, "domain.registration." + active.status)
        raise
