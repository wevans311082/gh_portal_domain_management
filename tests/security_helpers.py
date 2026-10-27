"""Explicit owner authentication for functional tests that exercise owner tools."""
import pyotp
from django.urls import reverse


def login_owner(client, user, password="test-owner-authentication-password!"):
    """Give a test account owner privileges, then complete the real two-factor flow.

    Functional tests using this helper exercise owner-only behavior. Authorization
    tests intentionally construct ordinary staff and do not use this helper.
    """
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.mfa_enabled = True
    user.mfa_secret = pyotp.random_base32()
    user.mfa_last_verified_step = -1
    user.set_password(password)
    user.save()
    from allauth.account.models import EmailAddress
    EmailAddress.objects.update_or_create(user=user, email=user.email, defaults={"verified": True, "primary": True})
    response = client.post(reverse("account_login"), {"email": user.email, "password": password})
    assert response.status_code == 302 and response.url == reverse("accounts_custom:mfa_verify")
    response = client.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(user.mfa_secret).now()})
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)
    return user
