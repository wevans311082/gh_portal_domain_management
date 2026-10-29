"""Direct registration drafts and agreement-gated billing, without registrar writes."""
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.billing.models import Invoice
from apps.billing.services import create_invoice, email_invoice_after_commit
from .agreements import assert_acceptance_current
from .models import Domain, DomainContact, DomainOrder, DomainTransfer, TLDPricing
from .publication_consent import capture_decision, publication_snapshot
from .registrar_policy import policy_for, tld_for_domain
from .registry_requirements import validate_registration
from .request_agreements import request_agreement_target
from .contact_validation import validate_order_contacts


def _available(domain_name):
    if (Domain.objects.filter(name__iexact=domain_name).exists()
            or DomainTransfer.objects.filter(domain_name__iexact=domain_name).exists()):
        raise ValidationError("This domain needs support review before another registration invoice can be created.")


def _customer(customer):
    current = User.objects.select_for_update().get(pk=customer.pk)
    if not current.is_active or current.is_staff or current.is_superuser:
        raise PermissionDenied("An active customer account is required.")
    return current


def validate_registration_invoice_source(order):
    """Caller holds the request/customer locks; freeze all used contact roles."""
    _available(order.domain_name)
    policy = policy_for(order.tld)
    contacts = {row.pk: row for row in DomainContact.objects.select_for_update().filter(
        pk__in={getattr(order, role + "_id") for role in policy.roles}).order_by("pk")}
    for role in policy.roles:
        setattr(order, role, contacts[getattr(order, role + "_id")])
    contact = contacts[order.registration_contact_id]
    validate_order_contacts(order, "1", order.tld)
    validate_registration(order.domain_name, contact, order.registration_years)
    publication_snapshot(order, contact)
    if order.total_price < 0:
        raise ValidationError("The recorded registration price needs support review.")
    return contact


@transaction.atomic
def create_registration_draft(*, customer, domain_name, contact_id, years, privacy_enabled, auto_renew,
                              dns_provider, publication_choice):
    customer = _customer(customer)
    contact = DomainContact.objects.select_for_update().filter(pk=contact_id, user=customer).first()
    if contact is None:
        raise ValidationError("Choose a registrant contact belonging to your account.")
    validate_registration(domain_name, contact, years)
    contact.as_resellerclub_payload("1", tld_for_domain(domain_name))
    _available(domain_name)
    pricing = TLDPricing.objects.filter(tld=tld_for_domain(domain_name), is_active=True).first()
    if pricing is None:
        raise ValidationError("Current registration pricing is unavailable. Contact support.")
    if DomainOrder.objects.filter(domain_name__iexact=domain_name).exists():
        raise ValidationError("A request already exists for this domain. Open My domains > Request agreements or contact support.")
    price = (pricing.registration_price * Decimal(years)).quantize(Decimal("0.01"))
    decision = capture_decision(customer=customer, contact=contact, domain_name=domain_name, choice=publication_choice)
    try:
        order = DomainOrder.objects.create(user=customer, domain_name=domain_name, tld=tld_for_domain(domain_name),
            registration_years=years, quoted_price=price, total_price=price, status=DomainOrder.STATUS_DRAFT,
            privacy_enabled=privacy_enabled, auto_renew=auto_renew, dns_provider=dns_provider,
            registration_contact=contact, admin_contact=contact, tech_contact=contact, billing_contact=contact,
            publication_decision=decision)
    except IntegrityError:
        # The transaction rolls back any consent/audit written during a racing duplicate.
        raise ValidationError("A request already exists for this domain. Reopen your request list.") from None
    AuditLog.objects.create(user=customer, action="domain.registration.draft_created", model_name="DomainOrder",
        object_id=str(order.pk), data={"operation": "registration"})
    return order


@transaction.atomic
def create_registration_invoice(*, customer, order_id, context_digest, plan_digest, subject_digest, send_email=True):
    order = DomainOrder.objects.select_for_update().filter(pk=order_id, user_id=customer.pk).first()
    if order is None:
        raise Http404
    customer = _customer(customer)
    if order.invoice_id:
        if order.invoice.user_id != customer.pk:
            raise ValidationError("The invoice association needs support review.")
        return order.invoice
    order, contact, operation, digest = request_agreement_target(customer, order_id, kind="registration", lock=True)
    if digest != context_digest:
        raise ValidationError("The request or price changed. Review its current details before creating an invoice.")
    if order.status != DomainOrder.STATUS_DRAFT:
        raise ValidationError("Support must arrange billing for this existing request. No invoice was created.")
    contact = validate_registration_invoice_source(order)
    evidence = assert_acceptance_current(receipt_id=order.agreement_acceptance_id, customer_id=customer.pk,
        contact_id=contact.pk, domain_name=order.domain_name, operation=operation)
    if evidence["plan_digest"] != plan_digest or evidence["subject_digest"] != subject_digest:
        raise ValidationError("The displayed agreements or contact changed. Review the current versions before invoicing.")
    # Preserve the direct registration route's existing source, price and VAT
    # contract; cart checkout continues using its own existing billing rules.
    invoice = create_invoice(user=customer, line_items=[{
        "description": f"Domain registration: {order.domain_name} ({order.registration_years} year(s))",
        "quantity": 1, "unit_price": order.total_price}], source_kind=Invoice.SOURCE_DOMAIN_REGISTRATION,
        vat_rate=Decimal("0.00"), due_date=timezone.now().date(), send_email=False)
    order.invoice, order.status = invoice, DomainOrder.STATUS_PENDING_PAYMENT
    order.save(update_fields=["invoice", "status", "updated_at"])
    AuditLog.objects.create(user=customer, action="domain.registration.invoice_created", model_name="DomainOrder",
        object_id=str(order.pk), data={"invoice_id": invoice.pk, "acceptance_id": evidence["acceptance_id"]})
    if send_email:
        email_invoice_after_commit(invoice)
    return invoice
