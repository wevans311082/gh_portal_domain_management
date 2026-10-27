"""SEC-01: real login flows, challenge binding, throttling and replay protection."""
from datetime import timedelta
from importlib import import_module

import pyotp
import pytest
from allauth.account.models import EmailAddress, Login
from allauth.account.internal.flows.login import perform_login, resume_login
from allauth.core.context import request_context
from django.conf import settings
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.adapter import PortalAccountAdapter
from apps.accounts.mfa import (
    CHALLENGE_SESSION_KEY, consume_backup_code, regenerate_backup_codes, verify_factor,
)
from apps.accounts.models import MFALoginChallenge, MFAAttemptWindow

pytestmark = pytest.mark.django_db
PASSWORD = "test-password-very-long!"


@pytest.fixture(autouse=True)
def reset_login_throttle():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def mfa_user(django_user_model):
    return django_user_model.objects.create_user(
        email="mfa-security@example.test", password=PASSWORD,
        mfa_enabled=True, mfa_secret=pyotp.random_base32(),
    )


def begin(client, user, **kwargs):
    response = client.post(reverse("account_login"), {"email": user.email, "password": PASSWORD, **kwargs})
    assert response.status_code == 302
    assert response.url == reverse("accounts_custom:mfa_verify")
    assert "_auth_user_id" not in client.session
    return MFALoginChallenge.objects.get(pk=client.session[CHALLENGE_SESSION_KEY])


def test_tests_and_normal_settings_share_the_backend_policy():
    assert settings.AUTHENTICATION_BACKENDS == import_module("cyberask_domains.settings.base").AUTHENTICATION_BACKENDS
    assert settings.AUTHENTICATION_BACKENDS == ["apps.accounts.backends.EmailBackend"]


@pytest.mark.parametrize("login_name", ["account_login", "accounts_custom:login"])
def test_password_only_never_completes_mfa_login(client, mfa_user, login_name):
    response = client.post(reverse(login_name), {"email": mfa_user.email, "password": PASSWORD})
    assert response.url == reverse("accounts_custom:mfa_verify")
    assert "_auth_user_id" not in client.session


def test_backend_rejects_password_only_but_accepts_an_unused_factor(mfa_user):
    assert authenticate(email=mfa_user.email, password=PASSWORD) is None
    code = pyotp.TOTP(mfa_user.mfa_secret).now()
    assert authenticate(username=mfa_user.email, password=PASSWORD, totp_token=code) == mfa_user
    assert authenticate(email=mfa_user.email, password=PASSWORD, totp_token=code) is None


def test_success_consumes_challenge_and_records_recent_mfa(client, mfa_user):
    challenge = begin(client, mfa_user, next="/portal/services/")
    old_key = client.session.session_key
    response = client.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(mfa_user.mfa_secret).now()})
    assert response.url == "/portal/services/"
    assert client.session["_auth_user_id"] == str(mfa_user.pk)
    assert CHALLENGE_SESSION_KEY not in client.session
    assert client.session["_mfa_verified_user_id"] == mfa_user.pk
    assert client.session.session_key != old_key
    challenge.refresh_from_db()
    assert challenge.consumed_at is not None


@pytest.mark.parametrize("destination", ["https://evil.example/", "//evil.example/", "javascript:alert(1)"])
def test_external_destinations_are_not_retained(client, mfa_user, destination):
    challenge = begin(client, mfa_user, next=destination)
    assert challenge.return_to == ""
    response = client.post(reverse("accounts_custom:mfa_verify"), {
        "code": pyotp.TOTP(mfa_user.mfa_secret).now(), "next": destination,
    })
    assert response.url == reverse("portal:dashboard")


def test_password_change_invalidates_pending_challenge(client, mfa_user):
    begin(client, mfa_user)
    mfa_user.set_password("a-different-secure-password")
    mfa_user.save(update_fields=["password"])
    response = client.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(mfa_user.mfa_secret).now()})
    assert response.url == reverse("accounts_custom:login")
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("change", ["inactive", "unverified", "disabled", "changed_secret"])
def test_account_security_changes_invalidate_pending_login(client, mfa_user, change):
    begin(client, mfa_user)
    old_secret = mfa_user.mfa_secret
    if change == "inactive":
        mfa_user.is_active = False
    elif change == "disabled":
        mfa_user.mfa_enabled = False
    elif change == "changed_secret":
        mfa_user.mfa_secret = pyotp.random_base32()
    else:
        EmailAddress.objects.create(user=mfa_user, email=mfa_user.email, verified=False)
    mfa_user.save()
    response = client.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(old_secret).now()})
    assert response.url == reverse("accounts_custom:login")
    assert "_auth_user_id" not in client.session


def test_expired_challenge_requires_password_again(client, mfa_user):
    challenge = begin(client, mfa_user)
    MFALoginChallenge.objects.filter(pk=challenge.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    response = client.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(mfa_user.mfa_secret).now()})
    assert response.url == reverse("accounts_custom:login")
    assert "_auth_user_id" not in client.session


def test_legacy_or_malformed_session_is_not_a_primary_factor(client, mfa_user):
    session = client.session
    session["_mfa_pending_user_id"] = mfa_user.pk
    session[CHALLENGE_SESSION_KEY] = "not-a-uuid"
    session.save()
    response = client.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(mfa_user.mfa_secret).now()})
    assert response.url == reverse("accounts_custom:login")
    assert "_auth_user_id" not in client.session


def test_invalid_form_submissions_are_throttled(client, mfa_user):
    challenge = begin(client, mfa_user)
    for _ in range(settings.MFA_MAX_ATTEMPTS):
        response = client.post(reverse("accounts_custom:mfa_verify"), {"code": ""})
        assert response.status_code == 200
    response = client.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(mfa_user.mfa_secret).now()})
    assert response.status_code == 303
    assert "Retry-After" in response
    assert "_auth_user_id" not in client.session
    challenge.refresh_from_db()
    assert challenge.attempts == settings.MFA_MAX_ATTEMPTS


def test_throttle_cannot_be_reset_by_starting_another_session(client, mfa_user):
    begin(client, mfa_user)
    for _ in range(settings.MFA_MAX_ATTEMPTS):
        client.post(reverse("accounts_custom:mfa_verify"), {"code": "bad-code"})
    other = Client()
    begin(other, mfa_user)
    response = other.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(mfa_user.mfa_secret).now()})
    assert response.status_code == 303
    assert "_auth_user_id" not in other.session
    MFAAttemptWindow.objects.filter(user=mfa_user).update(started_at=timezone.now() - timedelta(seconds=301))
    begin(other, mfa_user)
    response = other.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(mfa_user.mfa_secret).now()})
    assert response.url == reverse("portal:dashboard")


def test_totp_cannot_be_replayed_between_sessions(client, mfa_user):
    other = Client()
    begin(client, mfa_user)
    begin(other, mfa_user)
    code = pyotp.TOTP(mfa_user.mfa_secret).now()
    assert client.post(reverse("accounts_custom:mfa_verify"), {"code": code}).status_code == 302
    response = other.post(reverse("accounts_custom:mfa_verify"), {"code": code})
    assert response.status_code == 200
    assert "_auth_user_id" not in other.session


def test_backup_codes_are_single_use_across_instances(mfa_user):
    code = regenerate_backup_codes(mfa_user, count=1)[0]
    assert consume_backup_code(mfa_user, code)
    assert not consume_backup_code(mfa_user, code)


def test_future_window_token_prevents_older_token_replay(mfa_user):
    totp = pyotp.TOTP(mfa_user.mfa_secret)
    now = int(timezone.now().timestamp())
    assert verify_factor(mfa_user, totp.at(now + 30), allow_backup=False)
    assert not verify_factor(mfa_user, totp.at(now), allow_backup=False)


def test_restart_invalidates_previous_challenge(client, mfa_user):
    previous = begin(client, mfa_user)
    current = begin(client, mfa_user)
    previous.refresh_from_db()
    assert previous.consumed_at is not None
    assert previous.pk != current.pk


def allauth_request(client):
    request = RequestFactory().get("/accounts/confirm-email/")
    request.session = client.session
    from django.contrib.auth.models import AnonymousUser
    request.user = AnonymousUser()
    return request


@pytest.mark.parametrize("flow", [perform_login, resume_login])
def test_allauth_initial_and_resume_flows_cannot_bypass_mfa(client, mfa_user, flow):
    EmailAddress.objects.create(user=mfa_user, email=mfa_user.email, verified=True)
    request = allauth_request(client)
    with request_context(request):
        response = flow(request, Login(user=mfa_user, email_verification="none", redirect_url="/portal/services/"))
    assert response.url == reverse("accounts_custom:mfa_verify")
    assert "_auth_user_id" not in request.session
    assert request.session[CHALLENGE_SESSION_KEY]


def test_direct_allauth_adapter_login_is_gated(client, mfa_user):
    from allauth.core.exceptions import ImmediateHttpResponse
    request = allauth_request(client)
    with pytest.raises(ImmediateHttpResponse) as raised:
        PortalAccountAdapter().login(request, mfa_user)
    assert raised.value.response.url == reverse("accounts_custom:mfa_verify")
    assert "_auth_user_id" not in request.session


def test_admin_login_uses_shared_password_mfa_flow(client, mfa_user):
    mfa_user.is_staff = True
    mfa_user.is_superuser = True
    mfa_user.save(update_fields=["is_staff", "is_superuser"])
    response = client.post(reverse("admin:login"), {"username": mfa_user.email, "password": PASSWORD})
    assert response.status_code == 302
    assert response.url.startswith(reverse("account_login") + "?")
    assert "_auth_user_id" not in client.session
    begin(client, mfa_user, next=reverse("admin:index"))
    response = client.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(mfa_user.mfa_secret).now()})
    assert response.url == reverse("admin:index")
    assert client.get(reverse("admin:index")).status_code == 200


def test_inactive_mfa_account_never_starts_a_challenge(client, mfa_user):
    mfa_user.is_active = False
    mfa_user.save(update_fields=["is_active"])
    response = client.post(reverse("account_login"), {"email": mfa_user.email, "password": PASSWORD})
    assert response.status_code == 200
    assert CHALLENGE_SESSION_KEY not in client.session
    assert "_auth_user_id" not in client.session


def test_enabled_mfa_cannot_be_replaced_without_disabling(client, mfa_user):
    client.force_login(mfa_user)
    response = client.get(reverse("accounts_custom:mfa_setup"))
    assert response.url == reverse("accounts_custom:mfa_manage")
    assert "mfa_setup_secret" not in client.session


def test_legacy_sessions_are_invalidated(client, mfa_user):
    from django.contrib.auth.base_user import AbstractBaseUser
    client.force_login(mfa_user)
    session = client.session
    session["_auth_user_hash"] = AbstractBaseUser._get_session_auth_hash(mfa_user)
    session.save()
    response = client.get(reverse("accounts_custom:profile"))
    assert response.status_code == 302
    assert "_auth_user_id" not in client.session


def test_disable_and_reenable_cannot_restore_an_old_session(client, mfa_user):
    client.force_login(mfa_user)
    original_secret = mfa_user.mfa_secret
    mfa_user.mfa_enabled = False
    mfa_user.save(update_fields=["mfa_enabled"])
    mfa_user.mfa_enabled = True
    mfa_user.mfa_secret = original_secret
    mfa_user.save(update_fields=["mfa_enabled", "mfa_secret"])
    assert client.get(reverse("accounts_custom:profile")).status_code == 302
    assert "_auth_user_id" not in client.session


def test_challenge_is_bound_to_the_current_browser_session(client, mfa_user):
    begin(client, mfa_user)
    other = Client()
    response = other.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(mfa_user.mfa_secret).now()})
    assert response.url == reverse("accounts_custom:login")
    assert "_auth_user_id" not in other.session


def test_login_and_mfa_retain_csrf_protection(mfa_user):
    csrf_client = Client(enforce_csrf_checks=True)
    assert csrf_client.post(reverse("account_login"), {"email": mfa_user.email, "password": PASSWORD}).status_code == 403
    assert csrf_client.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(mfa_user.mfa_secret).now()}).status_code == 403


def test_mfa_page_is_not_cached_and_has_accessible_errors(client, mfa_user):
    begin(client, mfa_user)
    response = client.post(reverse("accounts_custom:mfa_verify"), {"code": "bad-code"})
    assert "no-store" in response["Cache-Control"]
    assert b'for="mfa-code"' in response.content
    assert b'aria-invalid="true"' in response.content
    assert b'role="alert"' in response.content


def test_confirmation_link_resume_requires_mfa(client, mfa_user):
    from allauth.account.models import EmailConfirmationHMAC
    address = EmailAddress.objects.create(user=mfa_user, email=mfa_user.email, verified=False, primary=True)
    login = Login(user=mfa_user, email_verification="mandatory", state={
        "stages": {"current": "verify_email"},
    })
    session = client.session
    session["account_login"] = login.serialize()
    session.save()
    key = EmailConfirmationHMAC(address).key
    response = client.post(reverse("account_confirm_email", args=[key]))
    address.refresh_from_db()
    assert address.verified
    assert response.url == reverse("accounts_custom:mfa_verify")
    assert "_auth_user_id" not in client.session
    assert client.session[CHALLENGE_SESSION_KEY]


def test_replayed_consumed_challenge_cannot_consume_another_factor(client, mfa_user):
    challenge = begin(client, mfa_user)
    backup = regenerate_backup_codes(mfa_user, count=2)
    client.post(reverse("accounts_custom:mfa_verify"), {"code": backup[0]})
    client.logout()
    session = client.session
    session[CHALLENGE_SESSION_KEY] = str(challenge.pk)
    session.save()
    response = client.post(reverse("accounts_custom:mfa_verify"), {"code": backup[1]})
    assert response.url == reverse("accounts_custom:login")
    assert consume_backup_code(mfa_user, backup[1])


def test_authentication_policy_check_detects_unsafe_backend(settings):
    from apps.accounts.checks import check_authentication_policy
    settings.AUTHENTICATION_BACKENDS = ["apps.accounts.backends.EmailBackend", "allauth.account.auth_backends.AuthenticationBackend"]
    assert [error.id for error in check_authentication_policy(None)] == ["accounts.E001"]


def test_mfa_enrollment_keeps_current_session_but_revokes_other_sessions(client, django_user_model):
    user = django_user_model.objects.create_user(email="enroll@example.test", password=PASSWORD)
    other = Client()
    client.force_login(user)
    other.force_login(user)
    client.get(reverse("accounts_custom:mfa_setup"))
    code = pyotp.TOTP(client.session["mfa_setup_secret"]).now()
    response = client.post(reverse("accounts_custom:mfa_setup"), {"token": code})
    assert response.url == reverse("accounts_custom:mfa_manage")
    assert client.get(reverse("accounts_custom:mfa_manage")).status_code == 200
    assert other.get(reverse("accounts_custom:profile")).status_code == 302


def test_mfa_disable_keeps_current_session_but_revokes_other_sessions(client, mfa_user):
    other = Client()
    client.force_login(mfa_user)
    other.force_login(mfa_user)
    response = client.post(reverse("accounts_custom:mfa_disable"), {"token": pyotp.TOTP(mfa_user.mfa_secret).now()})
    assert response.status_code == 302
    assert client.get(reverse("accounts_custom:profile")).status_code == 200
    assert other.get(reverse("accounts_custom:profile")).status_code == 302
