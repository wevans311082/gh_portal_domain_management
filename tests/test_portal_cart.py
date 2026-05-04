from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Invoice
from apps.billing.services import mark_invoice_paid
from apps.domains.models import Domain, DomainContact, DomainOrder, DomainRenewal, DomainTransfer, TLDPricing
from apps.portal.cart_service import add_domain_registration_item, create_quote_from_cart, get_active_cart
from apps.portal.models import PortalCart
from apps.products.models import Package
from apps.services.models import Service


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(email="cart@example.com", password="password123")


@pytest.fixture
def package():
    return Package.objects.create(
        name="Starter Hosting",
        slug="starter-hosting",
        price_monthly=Decimal("9.99"),
        price_annually=Decimal("99.99"),
        whm_package_name="starter_pkg",
    )


@pytest.fixture
def contact(user):
    return DomainContact.objects.create(
        user=user,
        label="Primary",
        name="Cart User",
        email=user.email,
        phone_country_code="44",
        phone="07123456789",
        address_line1="1 Cart Street",
        city="London",
        state="London",
        postcode="SW1A 1AA",
        country="GB",
        is_default=True,
    )


@pytest.fixture
def com_pricing():
    return TLDPricing.objects.create(
        tld="com",
        registration_cost=Decimal("8.00"),
        renewal_cost=Decimal("8.00"),
        transfer_cost=Decimal("8.00"),
        profit_margin_percentage=Decimal("25.00"),
        is_active=True,
    )


@pytest.fixture
def active_domain(user):
    return Domain.objects.create(
        user=user,
        name="renewme.com",
        tld="com",
        status=Domain.STATUS_ACTIVE,
        registrar_id="12345",
        expires_at=timezone.now().date(),
    )


@pytest.mark.django_db
@patch("apps.billing.services.email_document")
def test_cart_checkout_invoice_creates_invoice_and_pending_service(mock_email, client, user, package):
    client.force_login(user)

    response = client.post(
        reverse("portal:cart_add_hosting"),
        {
            "package_id": package.id,
            "billing_period": "monthly",
            "domain_name": "hosted.example.com",
        },
    )
    assert response.status_code == 302

    response = client.post(reverse("portal:cart_checkout_invoice"))
    assert response.status_code == 302

    invoice = Invoice.objects.get(user=user)
    service = Service.objects.get(user=user, invoice=invoice)
    submitted_cart = PortalCart.objects.get(invoice=invoice)
    active_cart = get_active_cart(user)

    assert invoice.source_kind == Invoice.SOURCE_SERVICE_ORDER
    assert service.package == package
    assert service.status == Service.STATUS_PENDING
    assert service.domain_name == "hosted.example.com"
    assert submitted_cart.status == PortalCart.STATUS_INVOICED
    assert active_cart.pk != submitted_cart.pk


@pytest.mark.django_db
@patch("apps.billing.services.email_document")
def test_cart_quote_acceptance_creates_invoice_and_domain_order(mock_email, client, user, contact, com_pricing):
    add_domain_registration_item(
        user=user,
        domain_name="quotecart.com",
        registration_years=2,
        domain_contact_id=contact.id,
        dns_provider="cloudflare",
    )
    cart = get_active_cart(user)
    quote = create_quote_from_cart(cart)

    client.force_login(user)
    response = client.post(reverse("billing_public:quote_public_accept", args=[quote.public_token]))
    assert response.status_code == 302

    invoice = Invoice.objects.get(source_quote=quote)
    order = DomainOrder.objects.get(invoice=invoice, domain_name="quotecart.com")
    cart.refresh_from_db()

    assert order.status == DomainOrder.STATUS_PENDING_PAYMENT
    assert order.registration_years == 2
    assert order.dns_provider == "cloudflare"
    assert cart.invoice == invoice
    assert cart.status == cart.STATUS_INVOICED


@pytest.mark.django_db
@patch("apps.provisioning.tasks.create_provisioning_job")
@patch("apps.billing.services.email_document")
def test_mark_invoice_paid_queues_pending_cart_services(mock_email, mock_create_job, client, user, package):
    client.force_login(user)
    client.post(
        reverse("portal:cart_add_hosting"),
        {
            "package_id": package.id,
            "billing_period": "annually",
            "domain_name": "paidcart.example.com",
        },
    )
    client.post(reverse("portal:cart_checkout_invoice"))

    invoice = Invoice.objects.get(user=user)
    service = Service.objects.get(user=user, invoice=invoice)
    assert service.status == Service.STATUS_PENDING

    mark_invoice_paid(invoice, send_email=False)

    invoice.refresh_from_db()
    service.refresh_from_db()

    assert invoice.status == Invoice.STATUS_PAID
    assert mock_create_job.call_count == 1
    assert mock_create_job.call_args.args[0].pk == service.pk


@pytest.mark.django_db
@patch("apps.billing.services.email_document")
def test_cart_checkout_invoice_creates_domain_renewal(mock_email, client, user, active_domain, com_pricing):
    client.force_login(user)

    response = client.post(
        reverse("portal:cart_add_renewal"),
        {
            "domain_id": active_domain.id,
            "renewal_years": 2,
        },
    )
    assert response.status_code == 302

    response = client.post(reverse("portal:cart_checkout_invoice"))
    assert response.status_code == 302

    invoice = Invoice.objects.get(user=user)
    renewal = DomainRenewal.objects.get(invoice=invoice, domain=active_domain)
    assert renewal.renewal_years == 2
    assert renewal.status == DomainRenewal.STATUS_PENDING_PAYMENT


@pytest.mark.django_db
@patch("apps.domains.tasks.execute_domain_transfer.delay")
@patch("apps.billing.services.email_document")
def test_cart_checkout_invoice_creates_domain_transfer_and_queues_it(mock_email, mock_transfer_delay, client, user, contact, com_pricing):
    client.force_login(user)

    response = client.post(
        reverse("portal:cart_add_transfer"),
        {
            "domain_name": "transferme.com",
            "auth_code": "AUTH-123",
            "domain_contact_id": contact.id,
            "dns_provider": "cloudflare",
            "auto_renew": "on",
        },
    )
    assert response.status_code == 302

    client.post(reverse("portal:cart_checkout_invoice"))
    invoice = Invoice.objects.get(user=user)
    transfer = DomainTransfer.objects.get(invoice=invoice, domain_name="transferme.com")
    assert transfer.auth_code == "AUTH-123"
    assert transfer.status == DomainTransfer.STATUS_PENDING_PAYMENT

    mark_invoice_paid(invoice, send_email=False)

    transfer.refresh_from_db()
    assert transfer.status == DomainTransfer.STATUS_PAID
    assert mock_transfer_delay.call_count == 1
