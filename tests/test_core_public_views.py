import pytest
from django.urls import reverse


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
