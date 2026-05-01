"""
Tests for apps/accounts views:
  - register
  - custom_login (password, MFA redirect, rate limiting)
  - mfa_verify
  - profile (GET/POST)
  - mfa_setup
  - mfa_disable
  - account_delete
"""
import pytest
import pyotp
from unittest.mock import patch
from django.urls import reverse
from django.core.cache import cache


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def make_user(django_user_model, email="user@test.com", password="pass1234!", is_active=True):
    return django_user_model.objects.create_user(
        email=email, password=password, is_active=is_active
    )


def make_mfa_user(django_user_model, email="mfa@test.com", password="pass1234!"):
    secret = pyotp.random_base32()
    user = django_user_model.objects.create_user(email=email, password=password)
    user.mfa_enabled = True
    user.mfa_secret = secret
    user.save(update_fields=["mfa_enabled", "mfa_secret"])
    return user, secret


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


# ─────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_register_get(client):
    response = client.get(reverse("accounts_custom:register"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_register_creates_user_and_logs_in(client, django_user_model):
    data = {
        "email": "newuser@test.com",
        "first_name": "New",
        "last_name": "User",
        "password1": "str0ngPass!99",
        "password2": "str0ngPass!99",
    }
    response = client.post(reverse("accounts_custom:register"), data)
    assert response.status_code == 302
    assert django_user_model.objects.filter(email="newuser@test.com").exists()


@pytest.mark.django_db
def test_register_invalid_form_stays_on_page(client):
    response = client.post(reverse("accounts_custom:register"), {"email": "bad"})
    assert response.status_code == 200


@pytest.mark.django_db
def test_register_redirects_authenticated_user(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    response = client.get(reverse("accounts_custom:register"))
    assert response.status_code == 302


# ─────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_login_get(client):
    response = client.get(reverse("accounts_custom:login"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_login_valid_credentials(client, django_user_model):
    make_user(django_user_model)
    response = client.post(reverse("accounts_custom:login"), {
        "email": "user@test.com",
        "password": "pass1234!",
    })
    assert response.status_code == 302
    assert response.url == reverse("portal:dashboard")


@pytest.mark.django_db
def test_login_wrong_password(client, django_user_model):
    make_user(django_user_model)
    response = client.post(reverse("accounts_custom:login"), {
        "email": "user@test.com",
        "password": "WRONG",
    })
    assert response.status_code == 200
    assert b"Invalid email or password" in response.content


@pytest.mark.django_db
def test_login_redirects_to_next(client, django_user_model):
    make_user(django_user_model)
    response = client.post(
        reverse("accounts_custom:login") + "?next=/support/",
        {"email": "user@test.com", "password": "pass1234!", "next": "/support/"},
    )
    assert response.status_code == 302
    assert "/support/" in response.url


@pytest.mark.django_db
def test_login_with_mfa_redirects_to_mfa_verify(client, django_user_model):
    user, _ = make_mfa_user(django_user_model)
    response = client.post(reverse("accounts_custom:login"), {
        "email": user.email,
        "password": "pass1234!",
    })
    assert response.status_code == 302
    assert response.url == reverse("accounts_custom:mfa_verify")


@pytest.mark.django_db
def test_login_rate_limit_blocks_after_max_attempts(client, django_user_model):
    make_user(django_user_model)
    for _ in range(5):
        client.post(reverse("accounts_custom:login"), {
            "email": "user@test.com",
            "password": "WRONG",
        })
    # 6th attempt should see rate limit message
    response = client.post(reverse("accounts_custom:login"), {
        "email": "user@test.com",
        "password": "WRONG",
    })
    assert b"Too many failed login attempts" in response.content


@pytest.mark.django_db
def test_login_already_authenticated_redirects(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    response = client.get(reverse("accounts_custom:login"))
    assert response.status_code == 302


# ─────────────────────────────────────────────
# MFA verify
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_mfa_verify_no_session_redirects(client):
    response = client.get(reverse("accounts_custom:mfa_verify"))
    assert response.status_code == 302


@pytest.mark.django_db
def test_mfa_verify_valid_token_logs_in(client, django_user_model):
    user, secret = make_mfa_user(django_user_model, email="mfav@test.com")
    session = client.session
    session["_mfa_pending_user_id"] = user.pk
    session.save()

    token = pyotp.TOTP(secret).now()
    response = client.post(reverse("accounts_custom:mfa_verify"), {"token": token})
    assert response.status_code == 302
    assert response.url == reverse("portal:dashboard")


@pytest.mark.django_db
def test_mfa_verify_invalid_token_shows_error(client, django_user_model):
    user, _ = make_mfa_user(django_user_model, email="mfabad@test.com")
    session = client.session
    session["_mfa_pending_user_id"] = user.pk
    session.save()

    response = client.post(reverse("accounts_custom:mfa_verify"), {"token": "000000"})
    assert response.status_code == 200
    assert b"Invalid code" in response.content


# ─────────────────────────────────────────────
# Profile
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_profile_requires_login(client):
    response = client.get(reverse("accounts_custom:profile"))
    assert response.status_code == 302


@pytest.mark.django_db
def test_profile_get_renders(client, django_user_model):
    user = make_user(django_user_model, email="profile@test.com")
    client.force_login(user)
    response = client.get(reverse("accounts_custom:profile"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_profile_post_updates_field(client, django_user_model):
    from apps.accounts.models import ClientProfile
    user = make_user(django_user_model, email="profilep@test.com")
    ClientProfile.objects.create(user=user)
    client.force_login(user)
    response = client.post(reverse("accounts_custom:profile"), {
        "address_line1": "1 Acme St",
        "city": "Townsville",
        "postcode": "AB1 2CD",
        "country": "GB",
    })
    assert response.status_code == 200
    profile = ClientProfile.objects.get(user=user)
    assert profile.city == "Townsville"


# ─────────────────────────────────────────────
# MFA Setup
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_mfa_setup_get_shows_qr(client, django_user_model):
    user = make_user(django_user_model, email="mfasetup@test.com")
    client.force_login(user)
    response = client.get(reverse("accounts_custom:mfa_setup"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_mfa_setup_valid_token_enables_mfa(client, django_user_model):
    user = make_user(django_user_model, email="mfaenable@test.com")
    client.force_login(user)

    # GET first to plant the secret in the session
    client.get(reverse("accounts_custom:mfa_setup"))
    session = client.session
    secret = session.get("mfa_setup_secret")
    assert secret is not None

    token = pyotp.TOTP(secret).now()
    response = client.post(reverse("accounts_custom:mfa_setup"), {"token": token})
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.mfa_enabled is True


@pytest.mark.django_db
def test_mfa_setup_wrong_token_stays_on_page(client, django_user_model):
    user = make_user(django_user_model, email="mfabadsetup@test.com")
    client.force_login(user)
    client.get(reverse("accounts_custom:mfa_setup"))

    response = client.post(reverse("accounts_custom:mfa_setup"), {"token": "000000"})
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.mfa_enabled is False


# ─────────────────────────────────────────────
# MFA Disable
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_mfa_disable_disables_mfa(client, django_user_model):
    user, secret = make_mfa_user(django_user_model, email="mfadis@test.com")
    client.force_login(user)
    token = pyotp.TOTP(secret).now()
    response = client.post(reverse("accounts_custom:mfa_disable"), {
        "token": token,
    })
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.mfa_enabled is False


@pytest.mark.django_db
def test_mfa_disable_wrong_token_rejected(client, django_user_model):
    user, _ = make_mfa_user(django_user_model, email="mfadisw@test.com")
    client.force_login(user)
    response = client.post(reverse("accounts_custom:mfa_disable"), {
        "token": "000000",
    })
    user.refresh_from_db()
    assert user.mfa_enabled is True


# ─────────────────────────────────────────────
# Account Delete
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_account_delete_get(client, django_user_model):
    user = make_user(django_user_model, email="del@test.com")
    client.force_login(user)
    response = client.get(reverse("accounts_custom:account_delete"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_account_delete_anonymises_user(client, django_user_model):
    user = make_user(django_user_model, email="todelete@test.com")
    client.force_login(user)
    response = client.post(reverse("accounts_custom:account_delete"), {
        "password": "pass1234!",
    })
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.is_active is False
    assert "deleted" in user.email


@pytest.mark.django_db
def test_account_delete_wrong_password_rejected(client, django_user_model):
    user = make_user(django_user_model, email="nodelete@test.com")
    client.force_login(user)
    response = client.post(reverse("accounts_custom:account_delete"), {
        "password": "WRONG",
    })
    user.refresh_from_db()
    assert user.is_active is True
