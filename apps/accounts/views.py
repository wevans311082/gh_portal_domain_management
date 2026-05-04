import io
import logging
import uuid

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, authenticate
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.audit.models import AuditLog
from .forms import RegistrationForm, ProfileUpdateForm, TOTPVerifyForm
from .models import ClientProfile, User

logger = logging.getLogger(__name__)

# Session key used to park a partially-authenticated user during MFA step
_MFA_USER_SESSION_KEY = "_mfa_pending_user_id"
_LOGIN_RATE_MAX = getattr(settings, "LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 5)
_LOGIN_RATE_WINDOW = getattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300)


def _login_rate_key(request) -> str:
    ip = (
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "unknown")
    )
    return f"login_rl:{ip}"


# ──────────────────────────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────────────────────────

def register(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    quote_token = request.GET.get("quote_token") or request.POST.get("quote_token") or request.session.get("pending_quote_token")
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            ClientProfile.objects.create(user=user)
            login(request, user, backend="apps.accounts.backends.EmailBackend")
            messages.success(request, "Account created successfully!")
            if quote_token:
                from django.urls import reverse
                request.session.pop("pending_quote_token", None)
                return redirect(reverse("billing_public:quote_public_accept_continue", args=[quote_token]))
            return redirect("portal:dashboard")
    else:
        form = RegistrationForm()
    return render(request, "accounts/register.html", {"form": form, "quote_token": quote_token})


# ──────────────────────────────────────────────────────────────────────────────
# Custom two-step login (password → optional MFA)
# ──────────────────────────────────────────────────────────────────────────────

def custom_login(request):
    """
    Step 1 of login: validate email + password.
    If the account has MFA enabled, park the user id in the session and
    redirect to the TOTP verification step rather than completing the login.
    """
    if request.user.is_authenticated:
        return redirect("portal:dashboard")

    next_url = request.GET.get("next", "") or request.POST.get("next", "")

    if request.method == "POST":
        # Rate limiting by IP
        rl_key = _login_rate_key(request)
        attempts = cache.get(rl_key, 0)
        if attempts >= _LOGIN_RATE_MAX:
            wait = _LOGIN_RATE_WINDOW // 60
            messages.error(request, f"Too many failed login attempts. Please wait {wait} minutes.")
            return render(request, "accounts/login.html", {"next": next_url, "rate_limited": True})

        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")

        # Authenticate without TOTP first to check password validity
        user = authenticate(request, email=email, password=password)

        if user is None:
            # Check if the user exists and has MFA — we need to differentiate
            # "wrong password" from "correct password but MFA required"
            try:
                db_user = User.objects.get(email__iexact=email)
                if db_user.mfa_enabled and db_user.check_password(password):
                    # Password correct, MFA required → redirect to TOTP step
                    request.session[_MFA_USER_SESSION_KEY] = db_user.pk
                    request.session.modified = True
                    return redirect("accounts_custom:mfa_verify")
            except User.DoesNotExist:
                pass

            # Increment failure counter
            cache.set(rl_key, attempts + 1, timeout=_LOGIN_RATE_WINDOW)
            messages.error(request, "Invalid email or password.")
            return render(request, "accounts/login.html", {"next": next_url})

        # No MFA — complete login immediately; reset failure counter
        cache.delete(rl_key)
        login(request, user, backend="apps.accounts.backends.EmailBackend")

        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        if user.is_staff:
            return redirect("admin_tools:dashboard")
        return redirect("portal:dashboard")

    return render(request, "accounts/login.html", {"next": next_url})


def mfa_verify(request):
    """
    Step 2 of login: validate TOTP token for users with MFA enabled.
    The user id is pulled from the session set in custom_login().
    """
    user_id = request.session.get(_MFA_USER_SESSION_KEY)
    if not user_id:
        return redirect("accounts_custom:login")

    try:
        pending_user = User.objects.get(pk=user_id, mfa_enabled=True)
    except User.DoesNotExist:
        del request.session[_MFA_USER_SESSION_KEY]
        return redirect("accounts_custom:login")

    next_url = request.GET.get("next", "") or request.POST.get("next", "")

    if request.method == "POST":
        form = TOTPVerifyForm(request.POST)
        if form.is_valid():
            token = form.cleaned_data["token"]
            totp = pyotp.TOTP(pending_user.mfa_secret)
            if totp.verify(token, valid_window=1):
                del request.session[_MFA_USER_SESSION_KEY]
                login(request, pending_user, backend="apps.accounts.backends.EmailBackend")
                if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                    return redirect(next_url)
                if pending_user.is_staff:
                    return redirect("admin_tools:dashboard")
                return redirect("portal:dashboard")
            else:
                logger.warning("Invalid MFA token for user %s", pending_user.pk)
                form.add_error("token", "Invalid code. Please try again.")
    else:
        form = TOTPVerifyForm()

    return render(request, "accounts/mfa_verify.html", {"form": form, "next": next_url})


# ──────────────────────────────────────────────────────────────────────────────
# Profile
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def profile(request):
    profile_obj, _ = ClientProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        form = ProfileUpdateForm(request.POST, instance=profile_obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")
    else:
        form = ProfileUpdateForm(instance=profile_obj)

    recent_activity = AuditLog.objects.filter(user=request.user).order_by("-created_at")[:20]
    return render(request, "accounts/profile.html", {
        "form": form,
        "recent_activity": recent_activity,
    })


# ──────────────────────────────────────────────────────────────────────────────
# MFA Enrollment
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def mfa_setup(request):
    """
    Generate a new TOTP secret and show the QR code for scanning.
    The secret is held in the session until the user confirms a valid code,
    at which point it is saved to the database and MFA is enabled.
    """
    user = request.user

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
                user.save(update_fields=["mfa_secret", "mfa_enabled"])
                del request.session["mfa_setup_secret"]
                messages.success(request, "Two-factor authentication has been enabled.")
                return redirect("accounts_custom:profile")
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
        issuer_name=request.META.get("HTTP_HOST", "Grumpy Hosting"),
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
    totp = pyotp.TOTP(user.mfa_secret)
    if totp.verify(token, valid_window=1):
        user.mfa_enabled = False
        user.mfa_secret = ""
        user.save(update_fields=["mfa_enabled", "mfa_secret"])
        messages.success(request, "Two-factor authentication has been disabled.")
    else:
        messages.error(request, "Invalid code. MFA has not been disabled.")
    return redirect("accounts_custom:profile")


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
