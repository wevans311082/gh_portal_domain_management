import pytest
from django.urls import reverse
from decimal import Decimal


@pytest.mark.django_db
def test_home_page_renders_without_reverse_errors(client):
    response = client.get(reverse("core:home"))

    assert response.status_code == 200
    assert "Contact Us" in response.content.decode()


@pytest.mark.django_db
def test_contact_page_renders(client):
    response = client.get(reverse("core:contact"))

    assert response.status_code == 200
    assert "Contact Us" in response.content.decode()


@pytest.mark.django_db
def test_pricing_page_shows_popular_tld_costs(client):
    from apps.products.models import Package
    from apps.domains.models import TLDPricing

    Package.objects.create(
        name="Starter",
        slug="starter-pricing-test",
        price_monthly=Decimal("3.00"),
        price_annually=Decimal("30.00"),
        whm_package_name="starter",
        is_active=True,
    )
    TLDPricing.objects.create(
        tld="com",
        currency="GBP",
        registration_cost=Decimal("8.00"),
        renewal_cost=Decimal("10.00"),
        is_active=True,
    )

    response = client.get(reverse("core:pricing"))
    body = response.content.decode()
    assert response.status_code == 200
    assert "Popular Domain Prices" in body
    assert ".com" in body
    assert "GBP" in body
