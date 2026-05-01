from decimal import Decimal

import pytest
from django.urls import reverse

from apps.billing.models import Invoice, InvoiceLineItem
from apps.domains.models import DomainContact, DomainOrder, DomainPricingSettings, TLDPricing


@pytest.mark.django_db
def test_register_view_creates_default_contact_for_user(client, django_user_model):
    user = django_user_model.objects.create_user(
        email="flow@example.com",
        password="password123",
        first_name="Flow",
        last_name="User",
    )
    pricing_settings = DomainPricingSettings.get_solo()
    pricing_settings.default_profit_margin_percentage = Decimal("20.00")
    pricing_settings.save(update_fields=["default_profit_margin_percentage"])
    TLDPricing.objects.create(tld="com", registration_cost=Decimal("10.00"))

    client.force_login(user)
    response = client.get(reverse("domains:register"), {"domain": "example.com"})

    assert response.status_code == 200
    assert DomainContact.objects.filter(user=user).count() == 1
    assert "GBP 12.00" in response.content.decode()


@pytest.mark.django_db
def test_register_view_creates_invoice_and_domain_order(client, django_user_model):
    user = django_user_model.objects.create_user(email="buyer@example.com", password="password123")
    pricing_settings = DomainPricingSettings.get_solo()
    pricing_settings.default_profit_margin_percentage = Decimal("20.00")
    pricing_settings.save(update_fields=["default_profit_margin_percentage"])
    TLDPricing.objects.create(tld="com", registration_cost=Decimal("10.00"))
    contact = DomainContact.objects.create(
        user=user,
        label="Primary",
        name="Buyer Example",
        email=user.email,
        phone_country_code="44",
        phone="07123456789",
        address_line1="1 Test Street",
        city="London",
        state="London",
        postcode="SW1A 1AA",
        country="GB",
        is_default=True,
    )

    client.force_login(user)
    response = client.post(
        reverse("domains:register"),
        {
            "domain_name": "example.com",
            "registration_years": "2",
            "contact": str(contact.id),
            "dns_provider": "cloudflare",
            "privacy_enabled": "on",
            "auto_renew": "on",
        },
    )

    order = DomainOrder.objects.get(domain_name="example.com")
    invoice = Invoice.objects.get(id=order.invoice_id)
    line_item = InvoiceLineItem.objects.get(invoice=invoice)

    assert response.status_code == 302
    assert response.url == reverse("payments:stripe_checkout", kwargs={"invoice_id": invoice.id})
    assert order.status == DomainOrder.STATUS_PENDING_PAYMENT
    assert order.total_price == Decimal("24.00")
    assert invoice.total == Decimal("24.00")
    assert line_item.description == "Domain registration: example.com (2 year(s))"
