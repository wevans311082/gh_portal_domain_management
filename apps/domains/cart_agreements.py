"""Customer agreement attachment; no invoice or provider side effects."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils.crypto import salted_hmac

from apps.audit.models import AuditLog
from apps.portal.models import PortalCart, PortalCartItem
from .agreements import _digest, assert_acceptance_current, record_agreement_acceptance
from .models import DomainContact


def cart_agreement_target(customer, item_id, *, lock=False):
    items = PortalCartItem.objects.filter(pk=item_id, cart__user_id=customer.pk,
        item_type__in=(PortalCartItem.TYPE_DOMAIN_REGISTRATION, PortalCartItem.TYPE_DOMAIN_TRANSFER))
    cart_id = items.values_list("cart_id", flat=True).first()
    if cart_id is None:
        raise Http404
    carts = PortalCart.objects.filter(pk=cart_id, user_id=customer.pk)
    if lock:
        carts = carts.select_for_update()
    cart = carts.first()
    if cart is None:
        raise Http404
    if lock:
        items = items.select_for_update()
    item = items.first()
    if item is None or item.cart_id != cart.pk:
        raise Http404
    if (cart.status not in (PortalCart.STATUS_ACTIVE, PortalCart.STATUS_QUOTED)
            or cart.invoice_id or (cart.status == PortalCart.STATUS_ACTIVE and cart.submitted_at)):
        raise ValidationError("This cart has already been submitted or closed. Contact support about its agreements.")
    if cart.status == PortalCart.STATUS_QUOTED:
        # Quote issuance records submitted_at too; it is not paid admission.
        # Do not invert quote-conversion's quote -> cart locking order here.
        quote = cart.quote
        if (quote is None or quote.user_id != customer.pk or quote.converted_invoice_id
                or quote.status not in {"draft", "sent", "viewed", "accepted"}):
            raise ValidationError("This cart's quote is closed or needs support review before accepting agreements.")
    contact = DomainContact.objects.filter(pk=item.domain_contact_id, user_id=customer.pk).first()
    if contact is None:
        raise ValidationError("Select an owned registrant contact by removing and re-adding this item. Manage your saved contacts first if needed.")
    operation = "registration" if item.item_type == PortalCartItem.TYPE_DOMAIN_REGISTRATION else "transfer"
    # Bind substantive request values without exposing the transfer secret in HTML or audit data.
    request_digest = _digest({"item": item.pk, "cart": cart.pk, "quote": cart.quote_id, "customer": customer.pk,
        "domain": item.domain_name, "operation": operation, "contact": contact.pk,
        "years": item.registration_years, "quantity": item.quantity, "price": str(item.unit_price),
        "privacy": item.privacy_enabled, "auto_renew": item.auto_renew, "dns": item.dns_provider,
        "publication": item.publication_decision_id, "auth_digest": _digest(item.transfer_auth_code)})
    digest = salted_hmac("cart-agreement-context-v1", request_digest, algorithm="sha256").hexdigest()
    item.cart = cart
    return item, contact, operation, digest


@transaction.atomic
def accept_cart_agreements(*, customer, item_id, context_digest, key, subject_digest, plan_digest, accepted):
    if not customer.is_active or customer.is_staff or customer.is_superuser:
        raise PermissionDenied
    item, contact, operation, current_digest = cart_agreement_target(customer, item_id, lock=True)
    if context_digest != current_digest:
        raise ValidationError("The cart item changed. Read its current details and agreements again.")
    receipt = record_agreement_acceptance(actor=customer, contact_id=contact.pk, domain_name=item.domain_name,
        operation=operation, key=key, subject_digest=subject_digest, plan_digest=plan_digest, accepted=accepted)
    # An old idempotent receipt must never overwrite a newer acceptance after publication changes.
    assert_acceptance_current(receipt_id=receipt.pk, customer_id=customer.pk, contact_id=contact.pk,
        domain_name=item.domain_name, operation=operation)
    if item.agreement_acceptance_id != receipt.pk:
        item.agreement_acceptance = receipt
        item.save(update_fields=["agreement_acceptance", "updated_at"])
        AuditLog.objects.create(user=customer, action="domain.agreement.attached", model_name="PortalCartItem",
            object_id=str(item.pk), data={"acceptance_id": receipt.pk, "operation": operation})
    return receipt
