import pytest
from decimal import Decimal
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Invoice
from apps.domains.models import Domain, DomainContact, DomainOrder, TLDPricing
from apps.products.models import Package


@pytest.mark.django_db
def test_manual_register_page_requires_staff(client, django_user_model):
    user = django_user_model.objects.create_user(email="c@example.com", password="x")
    client.force_login(user)
    resp = client.get(reverse("admin_tools:manual_domain_register"))
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
def test_manual_register_whm_package_dropdown_uses_snapshot(client, django_user_model, monkeypatch):
    from apps.provisioning.models import WHMPackageSnapshot

    staff = django_user_model.objects.create_user(email="admin-pkg@example.com", password="x", is_staff=True)
    client.force_login(staff)
    WHMPackageSnapshot.objects.create(name="starter", is_active=True)
    WHMPackageSnapshot.objects.create(name="business", is_active=True)

    class BoomWHM:
        def list_packages(self):
            raise RuntimeError("WHM offline")

    monkeypatch.setattr("apps.admin_tools.manual_order_views.WHMClient", BoomWHM)
    resp = client.get(reverse("admin_tools:manual_domain_register"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "starter" in body
    assert "business" in body


@pytest.mark.django_db
def test_manual_register_creates_domain_without_invoice(client, django_user_model, monkeypatch, settings):
    staff = django_user_model.objects.create_user(email="admin@example.com", password="x", is_staff=True)
    customer = django_user_model.objects.create_user(email="buyer@example.com", password="x")
    client.force_login(staff)

    settings.RESELLERCLUB_CUSTOMER_ID = "cust-1"
    settings.WHM_NAMESERVERS = ["ns1.example.com", "ns2.example.com"]
    TLDPricing.objects.create(
        tld="com",
        registration_cost=Decimal("8.00"),
        renewal_cost=Decimal("8.00"),
        profit_margin_percentage=Decimal("25.00"),
        is_active=True,
    )
    DomainContact.objects.create(
        user=customer,
        label="Primary",
        name="Buyer",
        email=customer.email,
        phone="07123456789",
        address_line1="1 Street",
        city="London",
        state="London",
        postcode="E1 1AA",
        country="GB",
        is_default=True,
    )

    class FakeRC:
        def create_contact(self, payload):
            return {"contact_id": 9}

        def update_contact(self, contact_id, payload):
            return {"contact_id": contact_id}

        def register_domain(self, **kwargs):
            return {"entityid": "999"}

        def modify_nameservers(self, order_id, nameservers):
            return {}

    monkeypatch.setattr("apps.domains.tasks.ResellerClubClient", lambda: FakeRC())

    resp = client.post(
        reverse("admin_tools:manual_domain_register"),
        {
            "domain_name": "cashsale.com",
            "user_id": str(customer.pk),
            "years": "1",
            "privacy_enabled": "on",
            "auto_renew": "on",
        },
    )
    assert resp.status_code == 302
    order = DomainOrder.objects.get(domain_name="cashsale.com")
    assert order.invoice_id is None
    assert order.status == DomainOrder.STATUS_COMPLETED
    assert Domain.objects.filter(name="cashsale.com", user=customer).exists()


@pytest.mark.django_db
def test_manual_generate_invoice_after_registration(client, django_user_model):
    staff = django_user_model.objects.create_user(email="admin2@example.com", password="x", is_staff=True)
    customer = django_user_model.objects.create_user(email="buyer2@example.com", password="x")
    client.force_login(staff)
    contact = DomainContact.objects.create(
        user=customer,
        label="P",
        name="B",
        email=customer.email,
        phone="07111",
        address_line1="1",
        city="L",
        state="L",
        postcode="E1",
        country="GB",
    )
    domain = Domain.objects.create(
        user=customer,
        name="postinv.com",
        tld="com",
        status=Domain.STATUS_ACTIVE,
        registered_at=timezone.now().date(),
    )
    order = DomainOrder.objects.create(
        user=customer,
        domain=domain,
        domain_name="postinv.com",
        tld="com",
        total_price=Decimal("15.00"),
        quoted_price=Decimal("15.00"),
        status=DomainOrder.STATUS_COMPLETED,
        registration_contact=contact,
        admin_contact=contact,
        tech_contact=contact,
        billing_contact=contact,
    )
    resp = client.post(
        reverse("admin_tools:manual_domain_generate_invoice", args=[order.pk]),
        {"amount": "20.00"},
    )
    assert resp.status_code == 302
    order.refresh_from_db()
    assert order.invoice_id
    assert order.invoice.subtotal == Decimal("20.00")
    assert order.invoice.status == Invoice.STATUS_UNPAID


@pytest.mark.django_db
def test_register_domain_order_allows_manual_paid_without_invoice(settings, django_user_model, monkeypatch):
    from apps.domains.tasks import register_domain_order

    settings.RESELLERCLUB_CUSTOMER_ID = "c1"
    settings.WHM_NAMESERVERS = ["ns1.example.com", "ns2.example.com"]
    user = django_user_model.objects.create_user(email="m@example.com", password="x")
    contact = DomainContact.objects.create(
        user=user,
        label="P",
        name="M",
        email=user.email,
        phone="07",
        address_line1="1",
        city="L",
        state="L",
        postcode="E1",
        country="GB",
    )
    order = DomainOrder.objects.create(
        user=user,
        invoice=None,
        domain_name="manualonly.com",
        tld="com",
        status=DomainOrder.STATUS_PAID,
        registration_contact=contact,
        admin_contact=contact,
        tech_contact=contact,
        billing_contact=contact,
    )

    class FakeRC:
        def create_contact(self, payload):
            return {"contact_id": 1}

        def update_contact(self, contact_id, payload):
            return {"contact_id": contact_id}

        def register_domain(self, **kwargs):
            return {"entityid": "1"}

        def modify_nameservers(self, *a, **k):
            return {}

    monkeypatch.setattr("apps.domains.tasks.ResellerClubClient", lambda: FakeRC())
    domain_id = register_domain_order.apply(args=[order.id]).get()
    order.refresh_from_db()
    assert order.status == DomainOrder.STATUS_COMPLETED
    assert Domain.objects.get(pk=domain_id).name == "manualonly.com"
