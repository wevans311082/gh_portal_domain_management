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
        raise ValueError("Invalid domain name.")

    pricing = TLDPricing.objects.filter(tld=tld, is_active=True).first()
    if not pricing:
        raise ValueError(f"Pricing is not available for .{tld}.")

    registration_years = max(1, min(int(registration_years), 10))
    total_price = (pricing.registration_price * Decimal(str(registration_years))).quantize(Decimal("0.01"))
    contact = None
    if domain_contact_id:
        contact = DomainContact.objects.filter(pk=domain_contact_id, user=user).first()
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
    pricing = TLDPricing.objects.filter(tld=domain.tld, is_active=True).first()
    if not pricing:
        raise ValueError(f"Renewal pricing is not available for .{domain.tld}.")

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
        raise ValueError("Invalid domain name.")

    pricing = TLDPricing.objects.filter(tld=tld, is_active=True).first()
    if not pricing:
        raise ValueError(f"Transfer pricing is not available for .{tld}.")

    contact = None
    if domain_contact_id:
        contact = DomainContact.objects.filter(pk=domain_contact_id, user=user).first()
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


@transaction.atomic
def materialize_cart_to_invoice(cart: PortalCart, invoice: Invoice) -> Invoice:
    items = list(cart.items.select_related("package", "domain_contact"))
    if not items:
        raise ValueError("Your cart is empty.")

    for item in items:
        if item.item_type == PortalCartItem.TYPE_HOSTING:
            if not item.package_id:
                raise ValueError("A hosting item is missing its package.")
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
                raise ValueError(f"Invalid domain in cart: {item.domain_name}")
            if Domain.objects.filter(name=item.domain_name).exists():
                raise ValueError(f"{item.domain_name} is already registered in your account.")
            if DomainOrder.objects.filter(domain_name=item.domain_name).exclude(status=DomainOrder.STATUS_FAILED).exists():
                raise ValueError(f"There is already an order in progress for {item.domain_name}.")
            contact = item.domain_contact or _default_domain_contact(invoice.user)
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
                    "privacy_enabled": item.privacy_enabled,
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
                raise ValueError("A renewal item is missing its domain.")
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
                raise ValueError(f"Invalid domain in cart: {item.domain_name}")
            if DomainTransfer.objects.filter(domain_name=item.domain_name).exclude(status=DomainTransfer.STATUS_FAILED).exists():
                raise ValueError(f"There is already a transfer in progress for {item.domain_name}.")
            contact = item.domain_contact or _default_domain_contact(invoice.user)
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
    line_items = build_line_items(cart)
    if not line_items:
        raise ValueError("Your cart is empty.")
    invoice = create_invoice(
        user=cart.user,
        line_items=line_items,
        source_kind=Invoice.SOURCE_SERVICE_ORDER,
        created_by_staff=cart.created_by_staff,
        send_email=send_email,
    )
    return materialize_cart_to_invoice(cart, invoice)


@transaction.atomic
def create_quote_from_cart(cart: PortalCart) -> Quote:
    line_items = build_line_items(cart)
    if not line_items:
        raise ValueError("Your cart is empty.")
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


def materialize_quote_cart_to_invoice(quote: Quote, invoice: Invoice) -> Invoice:
    cart = (
        PortalCart.objects.filter(quote=quote)
        .prefetch_related("items__package", "items__domain_contact")
        .order_by("-created_at")
        .first()
    )
    if not cart:
        return invoice
    return materialize_cart_to_invoice(cart, invoice)
