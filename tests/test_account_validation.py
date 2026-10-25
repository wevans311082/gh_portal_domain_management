"""Validation email after a password reset, and staff login overrides."""
import pytest
from allauth.account.forms import default_token_generator
from allauth.account.models import EmailAddress
from allauth.account.utils import user_pk_to_url_str
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.cache import SessionStore
from django.core import mail
from django.test import RequestFactory
from django.urls import reverse
from unittest.mock import patch

from apps.accounts.verification import (
    email_is_verified,
    send_validation_email_if_needed,
)


def _staff(django_user_model, email="staff-validate@example.com"):
    return django_user_model.objects.create_user(
        email=email, password="password123", is_staff=True
    )


def _user(django_user_model, email="client-validate@example.com", **kwargs):
    return django_user_model.objects.create_user(
        email=email, password="password123", **kwargs
    )


def _unverified(user):
    return EmailAddress.objects.create(
        user=user, email=user.email, primary=True, verified=False
    )


def _confirmation_messages():
    return [
        message
        for message in mail.outbox
        if "confirm" in (message.subject or "").lower()
        or "confirm" in (message.body or "").lower()
    ]


@pytest.mark.django_db
def test_validation_email_is_sent_only_for_an_unconfirmed_address(django_user_model):
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    request.session = SessionStore()
    request._messages = FallbackStorage(request)

    unverified = _user(django_user_model, "needs-confirm@example.com")
    _unverified(unverified)
    assert send_validation_email_if_needed(request, unverified) == "sent"
    assert len(_confirmation_messages()) == 1

    verified = _user(django_user_model, "already-ok@example.com")
    EmailAddress.objects.create(
        user=verified, email=verified.email, primary=True, verified=True
    )
    before = len(mail.outbox)
    assert send_validation_email_if_needed(request, verified) == "already_verified"
    assert len(mail.outbox) == before

    legacy = _user(django_user_model, "legacy@example.com")
    assert email_is_verified(legacy) is True
    assert send_validation_email_if_needed(request, legacy) == "already_verified"
    assert EmailAddress.objects.filter(user=legacy).count() == 0
    assert len(mail.outbox) == before


@pytest.mark.django_db
def test_admin_edit_shows_validation_and_login_overrides(client, django_user_model):
    staff = _staff(django_user_model)
    user = _user(django_user_model)
    _unverified(user)
    client.force_login(staff)

    response = client.get(reverse("admin_tools:user_edit", kwargs={"pk": user.pk}))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Not validated" in body
    assert "Blocked" in body
    assert "Mark account validated" in body
    assert "OK to login" in body
    assert 'name="action" value="mark_validated"' in body
    assert 'name="action" value="allow_login"' in body


@pytest.mark.django_db
def test_admin_mark_validated_lets_the_user_sign_in(client, django_user_model):
    staff = _staff(django_user_model)
    user = _user(django_user_model, "blocked@example.com")
    _unverified(user)
    client.force_login(staff)

    response = client.post(
        reverse("admin_tools:user_edit", kwargs={"pk": user.pk}),
        {"action": "mark_validated"},
    )

    assert response.status_code == 302
    record = EmailAddress.objects.get(user=user, email=user.email)
    assert record.verified is True
    assert record.primary is True

    login_client = client.__class__()
    login = login_client.post(
        reverse("accounts_custom:login"),
        {"email": user.email, "password": "password123"},
    )
    assert login.status_code == 302
    assert "_auth_user_id" in login_client.session


@pytest.mark.django_db
def test_admin_ok_to_login_activates_and_validates(client, django_user_model):
    staff = _staff(django_user_model)
    user = _user(django_user_model, "inactive@example.com", is_active=False)
    _unverified(user)
    client.force_login(staff)

    response = client.post(
        reverse("admin_tools:user_edit", kwargs={"pk": user.pk}),
        {"action": "allow_login"},
    )

    assert response.status_code == 302
    user.refresh_from_db()
    assert user.is_active is True
    assert EmailAddress.objects.get(user=user).verified is True

    login_client = client.__class__()
    login = login_client.post(
        reverse("accounts_custom:login"),
        {"email": user.email, "password": "password123"},
    )
    assert login.status_code == 302
    assert "_auth_user_id" in login_client.session


@pytest.mark.django_db
def test_mark_validated_leaves_an_inactive_account_inactive(client, django_user_model):
    staff = _staff(django_user_model)
    user = _user(django_user_model, "still-off@example.com", is_active=False)
    _unverified(user)
    client.force_login(staff)

    client.post(
        reverse("admin_tools:user_edit", kwargs={"pk": user.pk}),
        {"action": "mark_validated"},
    )

    user.refresh_from_db()
    assert user.is_active is False
    assert EmailAddress.objects.get(user=user).verified is True


@pytest.mark.django_db
def test_admin_password_change_sends_validation_email_when_unconfirmed(client, django_user_model):
    staff = _staff(django_user_model, "pw-staff-validate@example.com")
    user = _user(django_user_model, "pw-client-validate@example.com")
    _unverified(user)
    client.force_login(staff)

    with patch("apps.provisioning.whm_client.sync_user_cpanel_passwords", return_value=(0, [])):
        response = client.post(
            reverse("admin_tools:user_edit", kwargs={"pk": user.pk}),
            {
                "email": user.email,
                "first_name": "Pat",
                "last_name": "Client",
                "phone": "",
                "is_active": "on",
                "new_password1": "BrandNewPass123!",
                "new_password2": "BrandNewPass123!",
            },
        )

    assert response.status_code == 302
    user.refresh_from_db()
    assert user.check_password("BrandNewPass123!")
    assert EmailAddress.objects.get(user=user).verified is False
    assert len(_confirmation_messages()) == 1


@pytest.mark.django_db
def test_admin_password_reset_email_also_sends_validation(client, django_user_model):
    staff = _staff(django_user_model, "reset-staff@example.com")
    user = _user(django_user_model, "reset-client@example.com")
    _unverified(user)
    client.force_login(staff)

    response = client.post(
        reverse("admin_tools:user_edit", kwargs={"pk": user.pk}),
        {"action": "send_password_reset"},
    )

    assert response.status_code == 302
    subjects = " ".join(message.subject.lower() for message in mail.outbox)
    assert "password" in subjects
    assert len(_confirmation_messages()) == 1
    assert EmailAddress.objects.get(user=user).verified is False


@pytest.mark.django_db
def test_login_rejection_sends_the_validation_email(client, django_user_model):
    user = _user(django_user_model, "login-blocked@example.com")
    _unverified(user)

    response = client.post(
        reverse("accounts_custom:login"),
        {"email": user.email, "password": "password123"},
    )

    assert response.status_code == 200
    assert b"Confirm your email" in response.content
    assert "_auth_user_id" not in client.session
    assert len(_confirmation_messages()) == 1


@pytest.mark.django_db
def test_completing_password_reset_sends_validation_email(client, django_user_model):
    user = _user(django_user_model, "finish-reset@example.com")
    _unverified(user)
    uid = user_pk_to_url_str(user)
    token = default_token_generator.make_token(user)
    link = reverse(
        "account_reset_password_from_key",
        kwargs={"uidb36": uid, "key": token},
    )

    opened = client.get(link)
    assert opened.status_code == 302
    response = client.post(
        opened.url,
        {"password1": "BrandNewPass123!", "password2": "BrandNewPass123!"},
    )

    assert response.status_code == 302
    user.refresh_from_db()
    assert user.check_password("BrandNewPass123!")
    assert EmailAddress.objects.get(user=user).verified is False
    assert len(_confirmation_messages()) == 1


@pytest.mark.django_db
def test_verified_password_reset_does_not_send_another_confirmation(client, django_user_model):
    user = _user(django_user_model, "already-confirmed@example.com")
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    uid = user_pk_to_url_str(user)
    token = default_token_generator.make_token(user)
    link = reverse(
        "account_reset_password_from_key",
        kwargs={"uidb36": uid, "key": token},
    )

    opened = client.get(link)
    client.post(
        opened.url,
        {"password1": "BrandNewPass123!", "password2": "BrandNewPass123!"},
    )

    user.refresh_from_db()
    assert user.check_password("BrandNewPass123!")
    assert _confirmation_messages() == []
