import io
import logging
import uuid

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from django.utils.crypto import get_random_string

from apps.audit.models import AuditLog
from .forms import (
    MFALoginVerifyForm,
    MFARegenerateBackupCodesForm,
    PasswordChangePortalForm,
    ProfileUpdateForm,
    RegistrationForm,
    TOTPVerifyForm,
)
from .mfa import (
    active_backup_code_count, clear_challenge, finish_challenge,
    pending_challenge, regenerate_backup_codes, start_challenge, verify_factor,
)
from .models import ClientProfile, User

logger = logging.getLogger(__name__)

# Session key used to park a partially-authenticated user during MFA step
_LOGIN_RATE_MAX = getattr(settings, "LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 5)
_LOGIN_RATE_WINDOW = getattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300)
_SUPPORT_LOGIN_CODE_SESSION_KEY = "support_login_code"
_SUPPORT_LOGIN_CODE_TS_SESSION_KEY = "support_login_code_ts"


def _login_rate_key(request) -> str:
    from apps.core.http import get_client_ip

    ip = get_client_ip(request) or "unknown"
    return f"login_rl:{ip}"


def _resolve_support_email() -> str:
    try:
        from apps.core.models import SiteContentSettings

        email = (SiteContentSettings.get_solo().support_email or "").strip()
        if email:
            return email
    except Exception:
        pass
    return f"support@{getattr(settings, 'SITE_DOMAIN', 'example.com')}"


def _issue_support_login_code(request) -> str:
    existing = request.session.get(_SUPPORT_LOGIN_CODE_SESSION_KEY)
    issued_at = request.session.get(_SUPPORT_LOGIN_CODE_TS_SESSION_KEY)
    now = timezone.now().timestamp()

    if existing and issued_at and (now - float(issued_at)) < 900:
        return existing

    code = f"AUTH-{get_random_string(8, allowed_chars='ABCDEFGHJKLMNPQRSTUVWXYZ23456789')}"
    request.session[_SUPPORT_LOGIN_CODE_SESSION_KEY] = code
    request.session[_SUPPORT_LOGIN_CODE_TS_SESSION_KEY] = now
    request.session.modified = True
    return code


# ──────────────────────────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────────────────────────

def _email_is_verified(user) -> bool:
    from apps.accounts.verification import email_is_verified

    return email_is_verified(user)


def _reject_unverified(request, user, next_url, support_email, support_code):
    from apps.accounts.verification import validation_email_notice

    try:
        validation_email_notice(request, user)
    except Exception:
        logger.exception("Could not send a validation email to %s", user.email)
    messages.error(
        request,
        "Confirm your email address before signing in. Check your inbox for the link.",
    )
    return render(
        request,
        "accounts/login.html",
        {
            "next": next_url,
            "support_email": support_email,
            "support_login_code": support_code,
        },
    )


def register(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    quote_token = request.GET.get("quote_token") or request.POST.get("quote_token") or request.session.get("pending_quote_token")
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            ClientProfile.objects.create(user=user)
            from allauth.account.models import EmailAddress
            from allauth.account.internal.flows.email_verification import send_verification_email_to_address

            EmailAddress.objects.update_or_create(
                user=user,
                email=user.email,
                defaults={"primary": True, "verified": False},
            )
            try:
                address = EmailAddress.objects.get(user=user, email=user.email)
                send_verification_email_to_address(request, address, signup=True)
            except Exception:
                logger.error("Registration confirmation could not be queued user_id=%s", user.pk)
                messages.warning(request, "Your account was created, but its confirmation email could not be queued. Sign in later to request a new confirmation.")
            messages.success(
                request,
                "Account created. Confirm your email address before signing in.",
            )
            if quote_token:
                request.session["pending_quote_token"] = quote_token
            return redirect("account_login")
    else:
        form = RegistrationForm()
    return render(request, "accounts/register.html", {"form": form, "quote_token": quote_token})


# ──────────────────────────────────────────────────────────────────────────────
# Custom two-step login (password → optional MFA)
# ──────────────────────────────────────────────────────────────────────────────

@never_cache
def custom_login(request):
    """
    Step 1 of login: validate email + password.
    If the account has MFA enabled, park the user id in the session and
    redirect to the TOTP verification step rather than completing the login.
    """
    if request.user.is_authenticated:
        return redirect("portal:dashboard")

    next_url = request.GET.get("next", "") or request.POST.get("next", "")

    support_email = _resolve_support_email()
    support_code = _issue_support_login_code(request)

    if request.method == "POST":
        clear_challenge(request)
        # Rate limiting by IP
        rl_key = _login_rate_key(request)
        attempts = cache.get(rl_key, 0)
        if attempts >= _LOGIN_RATE_MAX:
            wait = _LOGIN_RATE_WINDOW // 60
            messages.error(request, f"Too many failed login attempts. Please wait {wait} minutes.")
            return render(
                request,
                "accounts/login.html",
                {
                    "next": next_url,
                    "rate_limited": True,
                    "support_email": support_email,
                    "support_login_code": support_code,
                },
            )

        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")

        from .backends import EmailBackend

        # Deliberately validate the primary factor, without creating a login.
        user = EmailBackend().authenticate_password(email, password)

        if user is None:
            # Increment failure counter
            cache.set(rl_key, attempts + 1, timeout=_LOGIN_RATE_WINDOW)
            messages.error(request, "Invalid email or password.")
            return render(
                request,
                "accounts/login.html",
                {
                    "next": next_url,
                    "support_email": support_email,
                    "support_login_code": support_code,
                },
            )

        if not _email_is_verified(user):
            return _reject_unverified(
                request,
                user,
                next_url,
                support_email,
                support_code,
            )

        if user.mfa_enabled:
            if not user.mfa_secret:
                messages.error(request, "Your authenticator setup needs account support.")
                return redirect("account_login")
            start_challenge(request, user, next_url)
            return redirect("accounts_custom:mfa_verify")

        # No MFA — complete login immediately; reset failure counter
        cache.delete(rl_key)
        login(request, user, backend="apps.accounts.backends.EmailBackend")

        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            return redirect(next_url)
        if user.is_staff:
            return redirect("admin_tools:dashboard")
        return redirect("portal:dashboard")

    return render(
        request,
        "accounts/login.html",
        {
            "next": next_url,
            "support_email": support_email,
            "support_login_code": support_code,
        },
    )


@never_cache
def mfa_verify(request):
    """
    Step 2 of login: validate TOTP token for users with MFA enabled.
    The user id is pulled from the session set in custom_login().
    """
    challenge = pending_challenge(request)
    if not challenge:
        messages.info(request, "Your sign-in challenge expired. Please sign in again.")
        return redirect("accounts_custom:login")
    pending_user = challenge.user
    next_url = challenge.return_to

    support_email = _resolve_support_email()
    support_code = _issue_support_login_code(request)

    if request.method == "POST":
        form = MFALoginVerifyForm(request.POST)
        valid_form = form.is_valid()
        # Empty/malformed submissions count towards the same database throttle.
        user, destination, used_backup, error = finish_challenge(
            request, form.cleaned_data["code"] if valid_form else ""
        )
        if user:
            login(request, user, backend="apps.accounts.backends.EmailBackend")
            cache.delete(_login_rate_key(request))
            if used_backup:
                messages.warning(request, "Signed in using a backup code. Generate a fresh set soon.")
            if destination:
                return redirect(destination)
            return redirect("admin_tools:dashboard" if user.is_staff else "portal:dashboard")
        if error == "expired":
            clear_challenge(request)
            messages.error(request, "Your sign-in challenge expired. Please sign in again.")
            return redirect("accounts_custom:login")
        if error == "limited":
            clear_challenge(request)
            messages.error(request, "Too many authentication-code attempts. Wait five minutes before signing in again.")
            response = redirect("accounts_custom:login")
            response.status_code = 303
            response["Retry-After"] = str(settings.MFA_RATE_LIMIT_WINDOW_SECONDS)
            return response
        if valid_form:
            logger.warning("Invalid MFA code for user %s", pending_user.pk)
            form.add_error("code", "Invalid authenticator or backup code. Please try again.")
    else:
        form = MFALoginVerifyForm()

    return render(
        request,
        "accounts/mfa_verify.html",
        {
            "form": form,
            "next": next_url,
            "backup_code_count": active_backup_code_count(pending_user),
            "support_email": support_email,
            "support_login_code": support_code,
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Profile
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def profile(request):
    profile_obj, _ = ClientProfile.objects.get_or_create(user=request.user)
    password_form = PasswordChangePortalForm(request.user)
    if request.method == "POST":
        action = (request.POST.get("action") or "profile").strip()
        if action == "password":
            password_form = PasswordChangePortalForm(request.user, request.POST)
            form = ProfileUpdateForm(instance=profile_obj)
            if password_form.is_valid():
                new_password = password_form.cleaned_data["new_password1"]
                request.user.set_password(new_password)
                request.user.save(update_fields=["password"])
                from django.contrib.auth import update_session_auth_hash

                update_session_auth_hash(request, request.user)
                if password_form.cleaned_data.get("sync_cpanel"):
                    from apps.provisioning.whm_client import sync_user_cpanel_passwords

                    updated, errors = sync_user_cpanel_passwords(request.user, new_password)
                    if updated:
                        messages.success(request, f"Password updated and synced to {updated} cPanel account(s).")
                    else:
                        messages.success(request, "Password updated.")
                    for err in errors:
                        messages.warning(request, err)
                else:
                    messages.success(request, "Password updated.")
                return redirect("accounts_custom:profile")
        else:
            form = ProfileUpdateForm(request.POST, instance=profile_obj)
            if form.is_valid():
                form.save()
                messages.success(request, "Profile updated.")
    else:
        form = ProfileUpdateForm(instance=profile_obj)

    recent_activity = AuditLog.objects.filter(user=request.user).order_by("-created_at")[:20]
    return render(request, "accounts/profile.html", {
        "form": form,
        "password_form": password_form,
        "recent_activity": recent_activity,
    })


# ──────────────────────────────────────────────────────────────────────────────
# MFA Enrollment
# ──────────────────────────────────────────────────────────────────────────────

@login_required
@never_cache
def mfa_setup(request):
    """
    Generate a new TOTP secret and show the QR code for scanning.
    The secret is held in the session until the user confirms a valid code,
    at which point it is saved to the database and MFA is enabled.
    """
    user = request.user
    if user.mfa_enabled:
        messages.info(request, "MFA is already enabled. Use the security settings to manage it.")
        return redirect("accounts_custom:mfa_manage")

    if request.method == "POST":
        form = TOTPVerifyForm(request.POST)
        if form.is_valid():
            secret = request.session.get("mfa_setup_secret")
            if not secret:
                messages.error(request, "Session expired. Please start the setup again.")
                return redirect("accounts_custom:mfa_setup")

            totp = pyotp.TOTP(secret)
            if totp.verify(form.cleaned_data["token"], valid_window=1):
                user.mfa_secret = secret
                user.mfa_enabled = True
                current_step = int(timezone.now().timestamp()) // totp.interval
                user.mfa_last_verified_step = next(
                    step for step in (current_step, current_step - 1, current_step + 1)
                    if totp.at(step * totp.interval) == form.cleaned_data["token"]
                )
                user.save(update_fields=["mfa_secret", "mfa_enabled", "mfa_last_verified_step"])
                update_session_auth_hash(request, user)
                fresh_codes = regenerate_backup_codes(user)
                request.session["fresh_mfa_backup_codes"] = fresh_codes
                del request.session["mfa_setup_secret"]
                messages.success(request, "Two-factor authentication has been enabled.")
                return redirect("accounts_custom:mfa_manage")
            else:
                form.add_error("token", "Invalid code — make sure your authenticator app is synced.")
    else:
        form = TOTPVerifyForm()

    # Generate (or reuse from session) a provisioning secret
    secret = request.session.get("mfa_setup_secret") or pyotp.random_base32()
    request.session["mfa_setup_secret"] = secret

    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(
        name=user.email,
        issuer_name=request.META.get("HTTP_HOST", "Cyber Ask Domains"),
    )

    # Render QR code as inline SVG
    qr = qrcode.make(provisioning_uri, image_factory=qrcode.image.svg.SvgPathImage)
    svg_buf = io.BytesIO()
    qr.save(svg_buf)
    qr_svg = svg_buf.getvalue().decode("utf-8")

    return render(request, "accounts/mfa_setup.html", {
        "form": form,
        "qr_svg": qr_svg,
        "secret": secret,
        "already_enabled": user.mfa_enabled,
    })


@login_required
@require_POST
def mfa_disable(request):
    """Disable MFA after re-confirming a valid TOTP token."""
    user = request.user
    if not user.mfa_enabled:
        messages.info(request, "MFA is not currently enabled.")
        return redirect("accounts_custom:profile")

    token = request.POST.get("token", "")
    if verify_factor(user, token, allow_backup=False):
        user.mfa_enabled = False
        user.mfa_secret = ""
        user.save(update_fields=["mfa_enabled", "mfa_secret"])
        update_session_auth_hash(request, user)
        user.mfa_backup_codes.all().delete()
        messages.success(request, "Two-factor authentication has been disabled.")
    else:
        messages.error(request, "Invalid code. MFA has not been disabled.")
    return redirect("accounts_custom:profile")


@login_required
@never_cache
def mfa_manage(request):
    user = request.user
    regenerate_form = MFARegenerateBackupCodesForm()

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "regenerate_codes":
            regenerate_form = MFARegenerateBackupCodesForm(request.POST)
            if not user.mfa_enabled:
                messages.error(request, "Enable MFA before generating backup codes.")
            elif regenerate_form.is_valid():
                token = regenerate_form.cleaned_data["token"]
                if verify_factor(user, token, allow_backup=False):
                    request.session["fresh_mfa_backup_codes"] = regenerate_backup_codes(user)
                    messages.success(request, "Backup codes regenerated. Save them now.")
                    return redirect("accounts_custom:mfa_manage")
                regenerate_form.add_error("token", "Invalid code. Backup codes were not regenerated.")

    fresh_codes = request.session.pop("fresh_mfa_backup_codes", None)

    return render(
        request,
        "accounts/mfa_manage.html",
        {
            "backup_code_count": active_backup_code_count(user),
            "fresh_backup_codes": fresh_codes,
            "regenerate_form": regenerate_form,
        },
    )


@login_required
@never_cache
def reauthenticate(request):
    from django import forms
    from apps.admin_tools.access import safe_return_url
    from .reauthentication import confirm_identity

    class IdentityForm(forms.Form):
        password = forms.CharField(label="Current password", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}), max_length=1024)
        code = forms.CharField(label="New authenticator code or unused backup code", max_length=16, widget=forms.TextInput(attrs={"autocomplete": "one-time-code"}))

    if request.session.get("impersonator_user_id"):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied("End customer impersonation before confirming your identity.")
    destination = safe_return_url(request, request.GET.get("next") or request.POST.get("next"),
        fallback=reverse("admin_tools:dashboard" if request.user.is_staff else "portal:dashboard"))
    form = IdentityForm(request.POST if request.method == "POST" else None)
    status = 200
    if request.method == "POST":
        valid = form.is_valid()
        result = confirm_identity(request, form.cleaned_data.get("password", "") if valid else "", form.cleaned_data.get("code", "") if valid else "")
        if not result:
            messages.success(request, "Identity confirmed. Submit your intended action again if needed.")
            return redirect(destination)
        if result == "limited":
            form.add_error(None, "Too many attempts. Wait five minutes before trying again.")
            status = 429
        elif result == "unavailable":
            form.add_error(None, "Enable MFA on your account before using protected actions.")
            status = 403
        else:
            form.add_error(None, "The password or authentication code was not accepted.")
    response = render(request, "accounts/reauthenticate.html", {
        "form": form, "next": destination, "mfa_enrolled": request.user.mfa_enabled,
    }, status=status)
    if status == 429:
        response["Retry-After"] = str(settings.MFA_RATE_LIMIT_WINDOW_SECONDS)
    return response


# ──────────────────────────────────────────────────────────────────────────────
# GDPR — Account deletion
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def account_delete(request):
    """
    Allow a user to request deletion of their account.
    Anonymises personal data rather than hard-deleting to preserve referential
    integrity on invoices, payments, and audit records.
    """
    if request.method == "POST":
        password = request.POST.get("password", "")
        if not request.user.check_password(password):
            messages.error(request, "Incorrect password. Account not deleted.")
            return render(request, "accounts/account_delete.html")

        user = request.user
        from django.contrib.auth import logout as auth_logout
        auth_logout(request)

        # Anonymise rather than hard-delete — preserves FK integrity
        from django.utils import timezone
        import uuid
        stub = f"deleted-{uuid.uuid4().hex[:8]}@deleted.invalid"
        user.email = stub
        user.first_name = "Deleted"
        user.last_name = "User"
        user.phone = ""
        user.is_active = False
        user.set_unusable_password()
        user.mfa_enabled = False
        user.mfa_secret = ""
        user.save()

        try:
            profile_obj = user.client_profile
            profile_obj.address_line1 = ""
            profile_obj.address_line2 = ""
            profile_obj.city = ""
            profile_obj.county = ""
            profile_obj.postcode = ""
            profile_obj.vat_number = ""
            profile_obj.stripe_customer_id = ""
            profile_obj.save()
        except Exception:
            pass

        logger.info("User %s account anonymised at their request.", user.pk)
        messages.success(request, "Your account has been deleted. Thank you for using our service.")
        return redirect("core:home")

    return render(request, "accounts/account_delete.html")
