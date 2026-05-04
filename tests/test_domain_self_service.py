"""Tests for Phase 2 domain self-service views:
  - domain_toggle_lock
  - domain_get_auth_code
  - domain_update_nameservers
  - domain_bulk_add_to_cart
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_user(django_user_model, email=None, is_staff=False):
    email = email or f"u{uuid.uuid4().hex[:6]}@example.com"
    return django_user_model.objects.create_user(
        email=email, password="pass1234!", is_staff=is_staff
    )


def make_domain(user, *, name="example.com", status="active", registrar_id="12345"):
    from apps.domains.models import Domain
    return Domain.objects.create(
        user=user,
        name=name,
        tld="com",
        status=status,
        registrar_id=registrar_id,
        is_locked=True,
        auto_renew=True,
    )


def make_tld_pricing(tld="com", renewal_cost="9.99"):
    from apps.domains.models import TLDPricing
    pricing, _ = TLDPricing.objects.get_or_create(
        tld=tld,
        defaults={
            "registration_cost": Decimal("9.99"),
            "renewal_cost": Decimal(renewal_cost),
            "transfer_cost": Decimal("9.99"),
            "is_active": True,
        },
    )
    return pricing


# ---------------------------------------------------------------------------
# domain_toggle_lock
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_toggle_lock_unlocks_domain(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user, status="active")
    assert domain.is_locked is True

    client.force_login(user)
    with patch("apps.domains.views.ResellerClubClient") as MockClient:
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance
        resp = client.post(reverse("domains:toggle_lock", kwargs={"pk": domain.pk}))

    assert resp.status_code == 302
    domain.refresh_from_db()
    assert domain.is_locked is False
    mock_instance.unlock_domain.assert_called_once_with(domain.registrar_id)


@pytest.mark.django_db
def test_toggle_lock_locks_domain(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user, status="active")
    domain.is_locked = False
    domain.save(update_fields=["is_locked"])

    client.force_login(user)
    with patch("apps.domains.views.ResellerClubClient") as MockClient:
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance
        resp = client.post(reverse("domains:toggle_lock", kwargs={"pk": domain.pk}))

    assert resp.status_code == 302
    domain.refresh_from_db()
    assert domain.is_locked is True
    mock_instance.lock_domain.assert_called_once_with(domain.registrar_id)


@pytest.mark.django_db
def test_toggle_lock_rejects_non_active_domain(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user, status="expired")

    client.force_login(user)
    resp = client.post(reverse("domains:toggle_lock", kwargs={"pk": domain.pk}))

    assert resp.status_code == 302
    domain.refresh_from_db()
    assert domain.is_locked is True  # unchanged


@pytest.mark.django_db
def test_toggle_lock_requires_login(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user)
    resp = client.post(reverse("domains:toggle_lock", kwargs={"pk": domain.pk}))
    assert resp.status_code == 302
    assert "/login/" in resp["Location"] or "/accounts/login/" in resp["Location"]


@pytest.mark.django_db
def test_toggle_lock_other_user_domain_404(client, django_user_model):
    owner = make_user(django_user_model)
    attacker = make_user(django_user_model)
    domain = make_domain(owner)

    client.force_login(attacker)
    resp = client.post(reverse("domains:toggle_lock", kwargs={"pk": domain.pk}))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# domain_get_auth_code
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_auth_code_stores_code(client, django_user_model):
    from django.core.cache import cache
    user = make_user(django_user_model)
    domain = make_domain(user, status="active")
    # clear any rate-limit key
    cache.delete(f"auth_code_request:{user.pk}:{domain.pk}")

    client.force_login(user)
    with patch("apps.domains.views.ResellerClubClient") as MockClient:
        mock_instance = MagicMock()
        mock_instance.get_auth_code.return_value = {"auth-code": "SECRETCODE123"}
        MockClient.return_value = mock_instance
        resp = client.post(reverse("domains:get_auth_code", kwargs={"pk": domain.pk}))

    assert resp.status_code == 302
    domain.refresh_from_db()
    assert domain.epp_code == "SECRETCODE123"


@pytest.mark.django_db
def test_get_auth_code_rate_limited(client, django_user_model):
    from django.core.cache import cache
    user = make_user(django_user_model)
    domain = make_domain(user, status="active")
    # Pre-set rate limit
    cache.set(f"auth_code_request:{user.pk}:{domain.pk}", True, 3600)

    client.force_login(user)
    with patch("apps.domains.views.ResellerClubClient") as MockClient:
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance
        resp = client.post(reverse("domains:get_auth_code", kwargs={"pk": domain.pk}))

    assert resp.status_code == 302
    mock_instance.get_auth_code.assert_not_called()


@pytest.mark.django_db
def test_get_auth_code_requires_active_domain(client, django_user_model):
    from django.core.cache import cache
    user = make_user(django_user_model)
    domain = make_domain(user, status="expired")
    cache.delete(f"auth_code_request:{user.pk}:{domain.pk}")

    client.force_login(user)
    resp = client.post(reverse("domains:get_auth_code", kwargs={"pk": domain.pk}))
    assert resp.status_code == 302
    domain.refresh_from_db()
    assert domain.epp_code == ""


# ---------------------------------------------------------------------------
# domain_update_nameservers
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_update_nameservers_success(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user, status="active")

    client.force_login(user)
    with patch("apps.domains.views.ResellerClubClient") as MockClient:
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance
        resp = client.post(
            reverse("domains:update_nameservers", kwargs={"pk": domain.pk}),
            {
                "nameserver1": "ns1.example.com",
                "nameserver2": "ns2.example.com",
                "nameserver3": "",
                "nameserver4": "",
            },
        )

    assert resp.status_code == 302
    domain.refresh_from_db()
    assert domain.nameserver1 == "ns1.example.com"
    assert domain.nameserver2 == "ns2.example.com"
    mock_instance.modify_nameservers.assert_called_once_with(
        domain.registrar_id, ["ns1.example.com", "ns2.example.com"]
    )


@pytest.mark.django_db
def test_update_nameservers_requires_at_least_two(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user, status="active")

    client.force_login(user)
    with patch("apps.domains.views.ResellerClubClient") as MockClient:
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance
        resp = client.post(
            reverse("domains:update_nameservers", kwargs={"pk": domain.pk}),
            {"nameserver1": "ns1.example.com", "nameserver2": ""},
        )

    assert resp.status_code == 302
    mock_instance.modify_nameservers.assert_not_called()


@pytest.mark.django_db
def test_update_nameservers_rejects_inactive_domain(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user, status="suspended")

    client.force_login(user)
    resp = client.post(
        reverse("domains:update_nameservers", kwargs={"pk": domain.pk}),
        {"nameserver1": "ns1.example.com", "nameserver2": "ns2.example.com"},
    )
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# domain_bulk_add_to_cart
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_bulk_add_to_cart_adds_multiple_domains(client, django_user_model):
    user = make_user(django_user_model)
    make_tld_pricing("com")
    d1 = make_domain(user, name="d1.com", status="active")
    d2 = make_domain(user, name="d2.com", status="active")

    client.force_login(user)
    resp = client.post(
        reverse("domains:bulk_add_to_cart"),
        {"domain_id": [str(d1.pk), str(d2.pk)], "renewal_years": "1"},
    )

    assert resp.status_code == 302
    assert resp["Location"].endswith(reverse("portal:cart"))

    from apps.portal.models import PortalCartItem, PortalCart
    cart = PortalCart.objects.get(user=user, status="active")
    assert cart.items.count() == 2


@pytest.mark.django_db
def test_bulk_add_to_cart_ignores_other_users_domains(client, django_user_model):
    user = make_user(django_user_model)
    other = make_user(django_user_model)
    make_tld_pricing("com")
    foreign_domain = make_domain(other, name="foreign.com", status="active")

    client.force_login(user)
    resp = client.post(
        reverse("domains:bulk_add_to_cart"),
        {"domain_id": [str(foreign_domain.pk)], "renewal_years": "1"},
    )

    assert resp.status_code == 302
    from apps.portal.models import PortalCart
    assert not PortalCart.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_bulk_add_to_cart_redirects_with_no_selection(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    resp = client.post(reverse("domains:bulk_add_to_cart"), {"domain_id": [], "renewal_years": "1"})
    assert resp.status_code == 302
    assert "my-domains" in resp["Location"]
