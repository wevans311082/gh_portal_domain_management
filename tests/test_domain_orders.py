from decimal import Decimal

import pytest
from django.utils import timezone

from apps.billing.models import Invoice
from apps.domains.models import DomainContact, DomainOrder, DomainPricingSettings
from apps.domains.tasks import register_domain_order
from apps.payments.views import _handle_checkout_completed


@pytest.mark.django_db
def test_contact_service_builds_defaults_from_profile(django_user_model):
    from apps.accounts.models import ClientProfile
    from apps.domains.services import DomainContactService

    user = django_user_model.objects.create_user(
        email="client@example.com",
        password="password123",
        first_name="Client",
        last_name="Person",
        phone="07123456789",
    )
    ClientProfile.objects.create(
        user=user,
        address_line1="1 Test Street",
        city="London",
        county="Greater London",
        postcode="SW1A 1AA",
        country="GB",
    )

    defaults = DomainContactService(client=None).build_default_contact(user)

    assert defaults["name"] == "Client Person"
    assert defaults["address_line1"] == "1 Test Street"
    assert defaults["country"] == "GB"


@pytest.mark.django_db
def test_register_domain_order_creates_domain_and_cloudflare_records(settings, django_user_model, monkeypatch):
    from apps.domains.models import Domain, DomainOrder
    from apps.dns.models import DNSRecord, DNSZone

    settings.RESELLERCLUB_CUSTOMER_ID = "customer-1"
    settings.PLATFORM_WWW_TARGET = "host.grumpyhosting.co.uk"
    settings.WHM_NAMESERVERS = ["ns1.grumpyhosting.co.uk", "ns2.grumpyhosting.co.uk"]
    pricing_settings = DomainPricingSettings.get_solo()
    pricing_settings.default_profit_margin_percentage = Decimal("20.00")
    pricing_settings.save(update_fields=["default_profit_margin_percentage"])

    user = django_user_model.objects.create_user(email="buyer@example.com", password="password123")
    invoice = Invoice.objects.create(
        user=user,
        number="INV-1000",
        status=Invoice.STATUS_PAID,
        total=Decimal("12.00"),
        amount_paid=Decimal("12.00"),
        paid_at=timezone.now(),
    )
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
    )
    order = DomainOrder.objects.create(
        user=user,
        invoice=invoice,
        domain_name="example.com",
        tld="com",
        registration_years=1,
        quoted_price=Decimal("12.00"),
        total_price=Decimal("12.00"),
        dns_provider=Domain.DNS_PROVIDER_CLOUDFLARE,
        registration_contact=contact,
        admin_contact=contact,
        tech_contact=contact,
        billing_contact=contact,
        status=DomainOrder.STATUS_PAID,
    )

    class FakeResellerClubClient:
        def create_contact(self, payload):
            return {"contact_id": 101}

        def update_contact(self, contact_id, payload):
            return {"contact_id": contact_id}

        def register_domain(self, **kwargs):
            return {"entityid": 555}

        def modify_nameservers(self, order_id, nameservers):
            return {"order-id": order_id, "ns": nameservers}

    class FakeCloudflareService:
        def create_zone(self, zone_name, jump_start=False):
            return {"result": {"id": "zone-1", "name_servers": ["ns1.cloudflare.test", "ns2.cloudflare.test"]}}

        def create_dns_record(self, zone_id, record_type, name, content, ttl=3600, proxied=False):
            return {"result": {"id": f"{record_type}-{name}"}}

    monkeypatch.setattr("apps.domains.tasks.ResellerClubClient", lambda: FakeResellerClubClient())
    monkeypatch.setattr("apps.domains.tasks.CloudflareService", lambda: FakeCloudflareService())

    result = register_domain_order.apply(args=[order.id]).get()

    order.refresh_from_db()
    domain = Domain.objects.get(name="example.com")
    zone = DNSZone.objects.get(domain=domain)
    www_record = DNSRecord.objects.get(zone=zone, name="www", record_type="CNAME")

    assert result == domain.id
    assert order.status == DomainOrder.STATUS_COMPLETED
    assert order.registrar_order_id == "555"
    assert domain.cloudflare_zone_id == "zone-1"
    assert www_record.content == "host.grumpyhosting.co.uk"


@pytest.mark.django_db
def test_checkout_completed_queues_paid_domain_orders(django_user_model, monkeypatch):
    user = django_user_model.objects.create_user(email="queue@example.com", password="password123")
    invoice = Invoice.objects.create(user=user, number="INV-2000", status=Invoice.STATUS_UNPAID, total=Decimal("10.00"))
    contact = DomainContact.objects.create(
        user=user,
        label="Primary",
        name="Queue Example",
        email=user.email,
        phone_country_code="44",
        phone="07123456789",
        address_line1="2 Queue Street",
        city="London",
        state="London",
        postcode="SW1A 2AA",
        country="GB",
    )
    order = DomainOrder.objects.create(
        user=user,
        invoice=invoice,
        domain_name="queue.com",
        tld="com",
        registration_years=1,
        quoted_price=Decimal("10.00"),
        total_price=Decimal("10.00"),
        registration_contact=contact,
        admin_contact=contact,
        tech_contact=contact,
        billing_contact=contact,
        status=DomainOrder.STATUS_PENDING_PAYMENT,
    )
    queued = []

    class FakeTask:
        def delay(self, order_id):
            queued.append(order_id)

    monkeypatch.setattr("apps.domains.tasks.register_domain_order", FakeTask())

    _handle_checkout_completed(
        {
            "metadata": {"invoice_id": str(invoice.id)},
            "amount_total": 1000,
            "currency": "gbp",
            "payment_intent": "pi_123",
        },
        webhook_event=None,
    )

    order.refresh_from_db()
    assert order.status == DomainOrder.STATUS_PAID
    assert queued == [order.id]
