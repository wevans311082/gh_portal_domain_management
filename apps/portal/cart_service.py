from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.billing.models import Invoice, Quote
from apps.billing.services import LineItemSpec, create_invoice, create_quote
from apps.domains.models import Domain, DomainContact, DomainOrder, DomainRenewal, DomainTransfer, TLDPricing
from apps.domains.services import DomainContactService
from apps.domains.views import _is_valid_label, _split_domain_name
from apps.portal.models import PortalCart, PortalCartItem
from apps.products.models import Package
from apps.services.models import Service


class CartPreflightError(ValueError):
    """Customer-correctable cart validation; safe to display with escaping."""
    def __init__(self, message, *, contact_id=None, issues=None):
        super().__init__(message)
        self.contact_id = contact_id
        self.issues = issues or []


def get_active_cart(user, *, created_by_staff=None) -> PortalCart:
    cart = (
        PortalCart.objects.filter(user=user, status=PortalCart.STATUS_ACTIVE, created_by_staff=created_by_staff)
        .prefetch_related("items__package", "items__domain_contact")
        .order_by("-created_at")
        .first()
    )
    if cart:
        return cart
    return PortalCart.objects.create(user=user, created_by_staff=created_by_staff)


def _default_domain_contact(user) -> DomainContact:
    contact = user.domain_contacts.filter(is_default=True).first() or user.domain_contacts.first()
    if contact:
        return contact
    return DomainContactService().ensure_default_contact(user)


def add_hosting_item(*, user, package_id: int, billing_period: str, domain_name: str = "", created_by_staff=None) -> PortalCartItem:
    package = Package.objects.get(pk=package_id, is_active=True)
    cart = get_active_cart(user, created_by_staff=created_by_staff)
    billing_period = billing_period if billing_period in dict(PortalCartItem.BILLING_PERIOD_CHOICES) else PortalCartItem.BILLING_MONTHLY
    domain_name = (domain_name or "").strip().lower()
    if not domain_name or "." not in domain_name:
        raise CartPreflightError("A primary domain name is required for hosting (e.g. example.com).")
    unit_price = package.price_annually if billing_period == PortalCartItem.BILLING_ANNUALLY else package.price_monthly
    description = f"{package.name} hosting ({'annual' if billing_period == PortalCartItem.BILLING_ANNUALLY else 'monthly'})"
    if domain_name:
        description = f"{description} for {domain_name}"
    return PortalCartItem.objects.create(
        cart=cart,
        item_type=PortalCartItem.TYPE_HOSTING,
        package=package,
        billing_period=billing_period,
        domain_name=domain_name,
        description=description,
        unit_price=unit_price,
        quantity=1,
        sort_order=cart.items.count(),
    )


def add_domain_registration_item(
    *,
    user,
    domain_name: str,
    registration_years: int = 1,
    domain_contact_id: int | None = None,
    privacy_enabled: bool = True,
    auto_renew: bool = True,
    dns_provider: str = Domain.DNS_PROVIDER_CPANEL,
    created_by_staff=None,
) -> PortalCartItem:
    cart = get_active_cart(user, created_by_staff=created_by_staff)
    normalized = (domain_name or "").strip().lower()
    label, tld = _split_domain_name(normalized)
    if not _is_valid_label(label):
        raise CartPreflightError("Invalid domain name.")

    pricing = TLDPricing.objects.filter(tld=tld, is_active=True).first()
    if not pricing:
        raise CartPreflightError(f"Pricing is not available for .{tld}.")

    registration_years = max(1, min(int(registration_years), 10))
    total_price = (pricing.registration_price * Decimal(str(registration_years))).quantize(Decimal("0.01"))
    contact = None
    if domain_contact_id:
        contact = DomainContact.objects.filter(pk=domain_contact_id, user=user).first()
        if contact is None:
            raise CartPreflightError("Choose a domain contact belonging to your account.")
    if contact is None:
        contact = _default_domain_contact(user)

    item, _created = PortalCartItem.objects.update_or_create(
        cart=cart,
        item_type=PortalCartItem.TYPE_DOMAIN_REGISTRATION,
        domain_name=normalized,
        defaults={
            "description": f"Domain registration: {normalized} ({registration_years} year(s))",
            "registration_years": registration_years,
            "unit_price": total_price,
            "quantity": 1,
            "domain_contact": contact,
            "privacy_enabled": privacy_enabled,
            "auto_renew": auto_renew,
            "dns_provider": dns_provider,
            "sort_order": cart.items.count(),
        },
    )
    return item


def add_domain_renewal_item(
    *,
    user,
    domain_id: int,
    renewal_years: int = 1,
    created_by_staff=None,
) -> PortalCartItem:
    cart = get_active_cart(user, created_by_staff=created_by_staff)
    domain = Domain.objects.get(pk=domain_id, user=user)
    from apps.domains.registrar_review import registrar_review_required, REVIEW_MESSAGE
    if registrar_review_required(domain):
        raise CartPreflightError(REVIEW_MESSAGE)
    pricing = TLDPricing.objects.filter(tld=domain.tld, is_active=True).first()
    if not pricing:
        raise CartPreflightError(f"Renewal pricing is not available for .{domain.tld}.")

    renewal_years = max(1, min(int(renewal_years), 10))
    total_price = (pricing.renewal_price * Decimal(str(renewal_years))).quantize(Decimal("0.01"))
    item, _created = PortalCartItem.objects.update_or_create(
        cart=cart,
        item_type=PortalCartItem.TYPE_DOMAIN_RENEWAL,
        domain=domain,
        defaults={
            "domain_name": domain.name,
            "description": f"Domain renewal: {domain.name} ({renewal_years} year(s))",
            "registration_years": renewal_years,
            "unit_price": total_price,
            "quantity": 1,
            "sort_order": cart.items.count(),
        },
    )
    return item


def add_domain_transfer_item(
    *,
    user,
    domain_name: str,
    auth_code: str = "",
    domain_contact_id: int | None = None,
    auto_renew: bool = True,
    dns_provider: str = Domain.DNS_PROVIDER_CPANEL,
    created_by_staff=None,
) -> PortalCartItem:
    cart = get_active_cart(user, created_by_staff=created_by_staff)
    normalized = (domain_name or "").strip().lower()
    label, tld = _split_domain_name(normalized)
    if not _is_valid_label(label):
        raise CartPreflightError("Invalid domain name.")

    pricing = TLDPricing.objects.filter(tld=tld, is_active=True).first()
    if not pricing:
        raise CartPreflightError(f"Transfer pricing is not available for .{tld}.")

    contact = None
    if domain_contact_id:
        contact = DomainContact.objects.filter(pk=domain_contact_id, user=user).first()
        if contact is None:
            raise CartPreflightError("Choose a domain contact belonging to your account.")
    if contact is None:
        contact = _default_domain_contact(user)

    item, _created = PortalCartItem.objects.update_or_create(
        cart=cart,
        item_type=PortalCartItem.TYPE_DOMAIN_TRANSFER,
        domain_name=normalized,
        defaults={
            "description": f"Domain transfer: {normalized}",
            "unit_price": pricing.transfer_price,
            "quantity": 1,
            "domain_contact": contact,
            "transfer_auth_code": (auth_code or "").strip(),
            "auto_renew": auto_renew,
            "dns_provider": dns_provider,
            "sort_order": cart.items.count(),
        },
    )
    return item


def remove_cart_item(*, user, item_id: int, created_by_staff=None) -> None:
    cart = get_active_cart(user, created_by_staff=created_by_staff)
    cart.items.filter(pk=item_id).delete()


def build_line_items(cart: PortalCart) -> list[LineItemSpec]:
    return [
        LineItemSpec(
            description=item.description,
            quantity=Decimal(str(item.quantity)),
            unit_price=item.unit_price,
            position=item.sort_order,
        )
        for item in cart.items.all()
    ]


def _service_next_due_date(billing_period: str):
    today = timezone.now().date()
    if billing_period == PortalCartItem.BILLING_ANNUALLY:
        return today + timezone.timedelta(days=365)
    return today + timezone.timedelta(days=30)


def _locked_cart(cart):
    current = PortalCart.objects.select_for_update().get(pk=cart.pk)
    if current.user_id != cart.user_id:
        raise CartPreflightError("The cart's account changed. Reopen your cart before checkout.")
    return current


@transaction.atomic
def preflight_cart_domains(cart: PortalCart):
    """Validate current owned contacts before allocating or emailing an invoice.

    Caller transactions retain these row locks through materialisation. Quotes
    may remain estimates; accepting a cart quote runs this check again.
    """
    from django.core.exceptions import ValidationError
    from apps.domains.registrar_policy import ALL_ROLES, policy_for

    current = _locked_cart(cart)
    items = list(current.items.select_for_update().order_by("pk"))
    if not items:
        raise CartPreflightError("Your cart is empty. Reopen it before checkout.")
    domain_items = [item for item in items if item.item_type in {
        PortalCartItem.TYPE_DOMAIN_REGISTRATION, PortalCartItem.TYPE_DOMAIN_TRANSFER,
    }]
    default = None
    if any(not item.domain_contact_id for item in domain_items):
        default = DomainContact.objects.filter(user_id=current.user_id).order_by("-is_default", "pk").first()
    selected = {item.pk: item.domain_contact_id or (default.pk if default else None) for item in domain_items}
    contacts = {contact.pk: contact for contact in DomainContact.objects.select_for_update().filter(
        pk__in=[pk for pk in selected.values() if pk], user_id=current.user_id,
    ).order_by("pk")}
    renewals = [item for item in items if item.item_type == PortalCartItem.TYPE_DOMAIN_RENEWAL]
    owned_domains = {domain.pk: domain for domain in Domain.objects.select_for_update().filter(
        pk__in=[item.domain_id for item in renewals if item.domain_id], user_id=current.user_id,
    ).order_by("pk")}
    if any(item.domain_id not in owned_domains for item in renewals):
        raise CartPreflightError("A renewal domain no longer belongs to this account. Remove it before checkout.")
    from apps.domains.registrar_review import REVIEW_MESSAGE, registrar_review_required
    if any(registrar_review_required(domain) for domain in owned_domains.values()):
        raise CartPreflightError(REVIEW_MESSAGE)
    result = {}
    for item in domain_items:
        if item.item_type == PortalCartItem.TYPE_DOMAIN_TRANSFER and (
                DomainTransfer.objects.filter(domain_name__iexact=item.domain_name).exists()
                or DomainOrder.objects.filter(domain_name__iexact=item.domain_name).exists()
                or Domain.objects.filter(name__iexact=item.domain_name).exists()):
            raise CartPreflightError("This domain needs support review before transfer. No new invoice was created.")
        if item.item_type == PortalCartItem.TYPE_DOMAIN_REGISTRATION and DomainTransfer.objects.filter(domain_name__iexact=item.domain_name).exists():
            raise CartPreflightError("This domain needs support review before registration. No new invoice was created.")
        contact = contacts.get(selected[item.pk])
        if contact is None:
            raise CartPreflightError("Choose an owned domain contact with a confirmed registrant type and accurate details before checkout. Manage your domain contacts to correct this.")
        label, tld = _split_domain_name(item.domain_name)
        if not _is_valid_label(label):
            raise CartPreflightError("A domain in your cart is invalid. Correct it before checkout.")
        try:
            policy = policy_for(tld)
        except ValidationError:
            raise CartPreflightError(f"{item.domain_name}: this extension needs a registrar eligibility review. Contact support before checkout.") from None
        if item.item_type == PortalCartItem.TYPE_DOMAIN_TRANSFER and policy.roles != ALL_ROLES:
            raise CartPreflightError("This extension requires a registrar-specific transfer workflow. Contact support before checkout.")
        try:
            contact.as_resellerclub_payload("1", tld)
        except ValidationError as exc:
            labels = {field.name: str(field.verbose_name).capitalize() for field in DomainContact._meta.fields}
            issues = [
                {"field": field, "label": labels.get(field, "Contact detail"), "message": message}
                for field, messages in exc.message_dict.items() for message in messages
            ]
            raise CartPreflightError(
                f"Update contact {contact.label} for {item.domain_name}.",
                contact_id=contact.pk, issues=issues,
            ) from None
        result[item.pk] = contact
    return result


@transaction.atomic
def preflight_quote_cart(quote: Quote):
    carts = list(PortalCart.objects.select_for_update().filter(quote=quote).order_by("pk")[:2])
    if len(carts) > 1:
        raise CartPreflightError("Multiple carts are linked to this quote. Contact support before accepting it.")
    cart = carts[0] if carts else None
    if cart:
        if cart.user_id != quote.user_id:
            raise CartPreflightError("The quote and cart belong to different accounts.")
        if cart.invoice_id:
            raise CartPreflightError("This cart has already been invoiced. Contact support before accepting the quote.")
        preflight_cart_domains(cart)
    return cart


@transaction.atomic
def materialize_cart_to_invoice(cart: PortalCart, invoice: Invoice) -> Invoice:
    from apps.domains.registrar_policy import policy_for
    cart = _locked_cart(cart)
    if cart.user_id != invoice.user_id:
        raise CartPreflightError("The cart and invoice belong to different accounts.")
    if cart.invoice_id:
        if cart.invoice_id == invoice.pk:
            return invoice
        raise CartPreflightError("This cart already belongs to another invoice.")
    contacts = preflight_cart_domains(cart)
    items = list(cart.items.select_related("package", "domain_contact"))
    if not items:
        raise CartPreflightError("Your cart is empty.")

    for item in items:
        if item.item_type == PortalCartItem.TYPE_HOSTING:
            if not item.package_id:
                raise CartPreflightError("A hosting item is missing its package.")
            Service.objects.get_or_create(
                user=invoice.user,
                invoice=invoice,
                package=item.package,
                domain_name=item.domain_name,
                billing_period=item.billing_period or PortalCartItem.BILLING_MONTHLY,
                defaults={
                    "status": Service.STATUS_PENDING,
                    "next_due_date": _service_next_due_date(item.billing_period),
                },
            )
            continue

        if item.item_type == PortalCartItem.TYPE_DOMAIN_REGISTRATION:
            label, tld = _split_domain_name(item.domain_name)
            if not _is_valid_label(label):
                raise CartPreflightError(f"Invalid domain in cart: {item.domain_name}")
            if Domain.objects.filter(name=item.domain_name).exists():
                raise CartPreflightError(f"{item.domain_name} is already registered in your account.")
            if DomainOrder.objects.filter(domain_name=item.domain_name).exclude(status=DomainOrder.STATUS_FAILED).exists():
                raise CartPreflightError(f"There is already an order in progress for {item.domain_name}.")
            contact = contacts[item.pk]
            DomainOrder.objects.get_or_create(
                invoice=invoice,
                domain_name=item.domain_name,
                defaults={
                    "user": invoice.user,
                    "tld": tld,
                    "registration_years": item.registration_years,
                    "quoted_price": item.unit_price,
                    "total_price": item.line_total,
                    "status": DomainOrder.STATUS_PENDING_PAYMENT,
                    "privacy_enabled": item.privacy_enabled and policy_for(tld).privacy_supported,
                    "auto_renew": item.auto_renew,
                    "dns_provider": item.dns_provider,
                    "registration_contact": contact,
                    "admin_contact": contact,
                    "tech_contact": contact,
                    "billing_contact": contact,
                },
            )
            continue

        if item.item_type == PortalCartItem.TYPE_DOMAIN_RENEWAL:
            if not item.domain_id:
                raise CartPreflightError("A renewal item is missing its domain.")
            DomainRenewal.objects.get_or_create(
                invoice=invoice,
                domain=item.domain,
                defaults={
                    "user": invoice.user,
                    "renewal_years": item.registration_years,
                    "total_price": item.line_total,
                    "status": DomainRenewal.STATUS_PENDING_PAYMENT,
                },
            )
            continue

        if item.item_type == PortalCartItem.TYPE_DOMAIN_TRANSFER:
            label, tld = _split_domain_name(item.domain_name)
            if not _is_valid_label(label):
                raise CartPreflightError(f"Invalid domain in cart: {item.domain_name}")
            if DomainTransfer.objects.filter(domain_name=item.domain_name).exists():
                raise CartPreflightError(f"There is already a transfer in progress for {item.domain_name}.")
            contact = contacts[item.pk]
            DomainTransfer.objects.get_or_create(
                invoice=invoice,
                domain_name=item.domain_name,
                defaults={
                    "user": invoice.user,
                    "tld": tld,
                    "auth_code": item.transfer_auth_code,
                    "quoted_price": item.unit_price,
                    "total_price": item.line_total,
                    "status": DomainTransfer.STATUS_PENDING_PAYMENT,
                    "auto_renew": item.auto_renew,
                    "dns_provider": item.dns_provider,
                    "registration_contact": contact,
                    "admin_contact": contact,
                    "tech_contact": contact,
                    "billing_contact": contact,
                },
            )

    cart.invoice = invoice
    cart.status = PortalCart.STATUS_INVOICED
    cart.submitted_at = timezone.now()
    cart.save(update_fields=["invoice", "status", "submitted_at", "updated_at"])
    return invoice


@transaction.atomic
def create_invoice_from_cart(cart: PortalCart, *, send_email: bool = True) -> Invoice:
    cart = _locked_cart(cart)
    if cart.invoice_id:
        if cart.invoice.user_id != cart.user_id:
            raise CartPreflightError("The cart and invoice belong to different accounts.")
        return cart.invoice
    if cart.status != PortalCart.STATUS_ACTIVE:
        raise CartPreflightError("This cart is no longer active. Accept its quote or open a new cart.")
    preflight_cart_domains(cart)
    line_items = build_line_items(cart)
    if not line_items:
        raise CartPreflightError("Your cart is empty.")
    invoice = create_invoice(
        user=cart.user,
        line_items=line_items,
        source_kind=Invoice.SOURCE_SERVICE_ORDER,
        created_by_staff=cart.created_by_staff,
        send_email=False,
    )
    invoice = materialize_cart_to_invoice(cart, invoice)
    if send_email:
        from apps.billing.services import email_invoice_after_commit
        email_invoice_after_commit(invoice)
    return invoice


@transaction.atomic
def create_quote_from_cart(cart: PortalCart) -> Quote:
    cart = _locked_cart(cart)
    if cart.invoice_id:
        raise CartPreflightError("This cart has already been invoiced. Open a new cart for another quote.")
    if cart.quote_id and cart.status == PortalCart.STATUS_QUOTED:
        if cart.quote.user_id != cart.user_id:
            raise CartPreflightError("The quote and cart belong to different accounts.")
        return cart.quote
    if cart.status != PortalCart.STATUS_ACTIVE:
        raise CartPreflightError("This cart is no longer active. Open a new cart.")
    line_items = build_line_items(cart)
    if not line_items:
        raise CartPreflightError("Your cart is empty.")
    quote = create_quote(
        user=cart.user,
        line_items=line_items,
        status=Quote.STATUS_SENT,
        created_by=cart.created_by_staff,
    )
    cart.quote = quote
    cart.status = PortalCart.STATUS_QUOTED
    cart.submitted_at = timezone.now()
    cart.save(update_fields=["quote", "status", "submitted_at", "updated_at"])
    return quote


@transaction.atomic
def materialize_quote_cart_to_invoice(quote: Quote, invoice: Invoice) -> Invoice:
    cart = preflight_quote_cart(quote)
    if not cart:
        return invoice
    return materialize_cart_to_invoice(cart, invoice)
