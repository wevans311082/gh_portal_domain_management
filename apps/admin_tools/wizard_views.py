"""
Setup wizard views.

The wizard is split into 7 steps:
  1. site       — site name, domain, timezone
  2. admin      — create the first superuser (if none exists)
  3. email      — outgoing mail configuration
  4. payments   — Stripe & GoCardless credentials
  5. registrar  — ResellerClub domain registrar
  6. hosting    — WHM / cPanel server
  7. cloudflare — Cloudflare API token

Each step writes values directly to the .env file (in development) or
validates connectivity, then marks itself complete in WizardProgress.
In production the view is locked to staff users only — it should be used
once at first-boot and then disabled via the SETUP_WIZARD_DISABLED env var.
"""
import logging
import os
import smtplib

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, redirect
import requests

from .models import IntegrationSetting, WizardProgress

logger = logging.getLogger(__name__)

BASE_DIR = settings.BASE_DIR
_ENV_PATH = BASE_DIR / ".env"

RESELLERCLUB_LIVE_API_URL = "https://httpapi.com/api"
RESELLERCLUB_TEST_API_URL = "https://test.httpapi.com/api"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _wizard_disabled() -> bool:
    return os.environ.get("SETUP_WIZARD_DISABLED", "").lower() in ("1", "true", "yes")


def _write_env_key(key: str, value: str):
    """Upsert a KEY=value line in the .env file."""
    if not _ENV_PATH.exists():
        _ENV_PATH.touch()

    lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
    updated = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            new_lines.append(f'{key}="{value}"')
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f'{key}="{value}"')
    _ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _read_env_key(key: str, default: str = "") -> str:
    db_value = IntegrationSetting.get_value(key, "")
    if db_value not in (None, ""):
        return db_value

    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            env_key, env_val = line.split("=", 1)
            if env_key.strip() == key:
                return env_val.strip().strip('"').strip("'")

    env_val = os.environ.get(key)
    if env_val not in (None, ""):
        return env_val

    setting_val = getattr(settings, key, "")
    if setting_val not in (None, ""):
        return str(setting_val)
    return default


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


# ──────────────────────────────────────────────────────────────────────────────
# Step forms
# ──────────────────────────────────────────────────────────────────────────────

class SiteSettingsForm(forms.Form):
    site_name = forms.CharField(
        max_length=100, label="Site / Company Name",
        initial=lambda: _read_env_key("SITE_NAME", "My Hosting"),
    )
    site_domain = forms.CharField(
        max_length=253, label="Primary Domain (e.g. example.com)",
        initial=lambda: _read_env_key("SITE_DOMAIN", ""),
    )
    time_zone = forms.ChoiceField(
        label="Timezone",
        choices=[
            ("Europe/London", "Europe/London"),
            ("UTC", "UTC"),
            ("America/New_York", "America/New_York"),
            ("America/Chicago", "America/Chicago"),
            ("America/Denver", "America/Denver"),
            ("America/Los_Angeles", "America/Los_Angeles"),
            ("Asia/Dubai", "Asia/Dubai"),
            ("Asia/Singapore", "Asia/Singapore"),
        ],
        initial="Europe/London",
    )
    admin_url_slug = forms.CharField(
        max_length=80, label="Admin URL slug (keep secret)",
        initial=lambda: _read_env_key("DJANGO_ADMIN_URL", "manage-site-a3f7c2/"),
        help_text="e.g. 'manage-x9z2p7/' — the URL where /[slug] leads to the admin panel.",
    )


class AdminUserForm(forms.Form):
    email = forms.EmailField(label="Admin email address")
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    password = forms.CharField(label="Password", widget=forms.PasswordInput, min_length=12)
    confirm_password = forms.CharField(label="Confirm Password", widget=forms.PasswordInput)

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password")
        p2 = cleaned.get("confirm_password")
        if p1 and p2 and p1 != p2:
            self.add_error("confirm_password", "Passwords do not match.")
        return cleaned


class EmailSettingsForm(forms.Form):
    email_host = forms.CharField(max_length=255, label="SMTP Host", required=False)
    email_port = forms.IntegerField(label="SMTP Port", initial=587)
    email_use_tls = forms.BooleanField(label="Use TLS", required=False, initial=True)
    email_host_user = forms.CharField(max_length=255, label="SMTP Username", required=False)
    email_host_password = forms.CharField(
        max_length=255, label="SMTP Password", widget=forms.PasswordInput, required=False,
    )
    default_from_email = forms.EmailField(
        label="Default From Address",
        initial=lambda: _read_env_key("DEFAULT_FROM_EMAIL", ""),
        required=False,
    )


class PaymentsSettingsForm(forms.Form):
    stripe_publishable_key = forms.CharField(
        max_length=255, label="Stripe Publishable Key", required=False,
    )
    stripe_secret_key = forms.CharField(
        max_length=255, label="Stripe Secret Key", widget=forms.PasswordInput, required=False,
    )
    stripe_webhook_secret = forms.CharField(
        max_length=255, label="Stripe Webhook Secret", widget=forms.PasswordInput, required=False,
    )
    gocardless_access_token = forms.CharField(
        max_length=255, label="GoCardless Access Token", widget=forms.PasswordInput, required=False,
    )
    gocardless_webhook_secret = forms.CharField(
        max_length=255, label="GoCardless Webhook Secret", widget=forms.PasswordInput, required=False,
    )
    gocardless_environment = forms.ChoiceField(
        choices=[("sandbox", "Sandbox (testing)"), ("live", "Live")],
        initial="sandbox",
        label="GoCardless Environment",
    )


class RegistrarSettingsForm(forms.Form):
    resellerclub_reseller_id = forms.CharField(
        max_length=50,
        label="Reseller ID",
        required=False,
        help_text=(
            "Your ResellerClub <strong>Reseller</strong> account ID — used to authenticate every API call. "
            "Find it under My Account → Personal Information in the ResellerClub control panel."
        ),
    )
    resellerclub_customer_id = forms.CharField(
        max_length=50,
        label="Default Customer ID",
        required=False,
        help_text=(
            "A <strong>Customer</strong> account under your reseller used for domain registrations. "
            "This is <em>different</em> from your Reseller ID — create or find it under Customers in the control panel. "
            "Typically you create a single \u2018master\u2019 customer account to act on behalf of."
        ),
    )
    resellerclub_api_key = forms.CharField(
        max_length=255, label="API Key", widget=forms.PasswordInput(render_value=True), required=False,
        help_text="Generate under My Account \u2192 API in the ResellerClub control panel.",
    )
    resellerclub_api_mode = forms.ChoiceField(
        label="API Environment",
        choices=[
            ("live", "Live (production)"),
            ("test", "Test / sandbox"),
            ("custom", "Custom URL"),
        ],
        initial="live",
    )
    resellerclub_api_url = forms.URLField(
        label="API Base URL",
        initial=RESELLERCLUB_LIVE_API_URL,
        help_text="Live default: https://httpapi.com/api, Test default: https://test.httpapi.com/api",
        required=False,
    )

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("resellerclub_api_mode")
        custom_url = (cleaned.get("resellerclub_api_url") or "").strip()

        if mode == "live":
            cleaned["resellerclub_api_url"] = RESELLERCLUB_LIVE_API_URL
        elif mode == "test":
            cleaned["resellerclub_api_url"] = RESELLERCLUB_TEST_API_URL
        elif mode == "custom":
            if not custom_url:
                self.add_error("resellerclub_api_url", "Custom API URL is required when mode is Custom.")
        return cleaned


class HostingSettingsForm(forms.Form):
    whm_host = forms.CharField(max_length=255, label="WHM Hostname / IP", required=False)
    whm_port = forms.IntegerField(label="WHM Port", initial=2087)
    whm_username = forms.CharField(max_length=100, label="WHM Username", initial="root", required=False)
    whm_api_token = forms.CharField(
        max_length=512, label="WHM API Token", widget=forms.PasswordInput, required=False,
    )


class CloudflareSettingsForm(forms.Form):
    cloudflare_api_token = forms.CharField(
        max_length=255, label="Cloudflare API Token", widget=forms.PasswordInput, required=False,
    )
    cloudflare_email = forms.EmailField(label="Cloudflare Account Email", required=False)


# ──────────────────────────────────────────────────────────────────────────────
# Step metadata
# ──────────────────────────────────────────────────────────────────────────────

STEP_META = {
    WizardProgress.STEP_SITE: {
        "title": "Site Settings",
        "description": "Configure your hosting portal's basic identity.",
        "icon": "🌐",
        "form_class": SiteSettingsForm,
    },
    WizardProgress.STEP_ADMIN: {
        "title": "Create Admin Account",
        "description": "Set up the first administrator account.",
        "icon": "👤",
        "form_class": AdminUserForm,
    },
    WizardProgress.STEP_EMAIL: {
        "title": "Email / SMTP",
        "description": "Configure outgoing email for invoices, notifications and password resets.",
        "icon": "✉️",
        "form_class": EmailSettingsForm,
    },
    WizardProgress.STEP_PAYMENTS: {
        "title": "Payment Gateways",
        "description": "Connect Stripe and/or GoCardless to accept payments.",
        "icon": "💳",
        "form_class": PaymentsSettingsForm,
    },
    WizardProgress.STEP_REGISTRAR: {
        "title": "Domain Registrar",
        "description": "Connect your ResellerClub / LogicBoxes account.",
        "icon": "🔗",
        "form_class": RegistrarSettingsForm,
    },
    WizardProgress.STEP_HOSTING: {
        "title": "Hosting Server (WHM/cPanel)",
        "description": "Connect to your WHM server for automatic account provisioning.",
        "icon": "🖥️",
        "form_class": HostingSettingsForm,
    },
    WizardProgress.STEP_CLOUDFLARE: {
        "title": "Cloudflare",
        "description": "Integrate Cloudflare for DNS and CDN management.",
        "icon": "☁️",
        "form_class": CloudflareSettingsForm,
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Env-key mappings per step
# ──────────────────────────────────────────────────────────────────────────────

_STEP_ENV_KEYS = {
    WizardProgress.STEP_SITE: {
        "site_name": "SITE_NAME",
        "site_domain": "SITE_DOMAIN",
        "time_zone": "DJANGO_TIME_ZONE",
        "admin_url_slug": "DJANGO_ADMIN_URL",
    },
    WizardProgress.STEP_EMAIL: {
        "email_host": "EMAIL_HOST",
        "email_port": "EMAIL_PORT",
        "email_use_tls": "EMAIL_USE_TLS",
        "email_host_user": "EMAIL_HOST_USER",
        "email_host_password": "EMAIL_HOST_PASSWORD",
        "default_from_email": "DEFAULT_FROM_EMAIL",
    },
    WizardProgress.STEP_PAYMENTS: {
        "stripe_publishable_key": "STRIPE_PUBLISHABLE_KEY",
        "stripe_secret_key": "STRIPE_SECRET_KEY",
        "stripe_webhook_secret": "STRIPE_WEBHOOK_SECRET",
        "gocardless_access_token": "GOCARDLESS_ACCESS_TOKEN",
        "gocardless_webhook_secret": "GOCARDLESS_WEBHOOK_SECRET",
        "gocardless_environment": "GOCARDLESS_ENVIRONMENT",
    },
    WizardProgress.STEP_REGISTRAR: {
        "resellerclub_reseller_id": "RESELLERCLUB_RESELLER_ID",
        "resellerclub_customer_id": "RESELLERCLUB_CUSTOMER_ID",
        "resellerclub_api_key": "RESELLERCLUB_API_KEY",
        "resellerclub_api_url": "RESELLERCLUB_API_URL",
    },
    WizardProgress.STEP_HOSTING: {
        "whm_host": "WHM_HOST",
        "whm_port": "WHM_PORT",
        "whm_username": "WHM_USERNAME",
        "whm_api_token": "WHM_API_TOKEN",
    },
    WizardProgress.STEP_CLOUDFLARE: {
        "cloudflare_api_token": "CLOUDFLARE_API_TOKEN",
        "cloudflare_email": "CLOUDFLARE_EMAIL",
    },
}


_STEP_FIELD_DEFAULTS = {
    WizardProgress.STEP_SITE: {
        "site_name": "My Hosting",
        "site_domain": "",
        "time_zone": "Europe/London",
        "admin_url_slug": "manage-site-a3f7c2/",
    },
    WizardProgress.STEP_EMAIL: {
        "email_port": "587",
        "email_use_tls": "true",
    },
    WizardProgress.STEP_PAYMENTS: {
        "gocardless_environment": "sandbox",
    },
    WizardProgress.STEP_REGISTRAR: {
        "resellerclub_api_url": RESELLERCLUB_LIVE_API_URL,
    },
    WizardProgress.STEP_HOSTING: {
        "whm_port": "2087",
        "whm_username": "root",
    },
}


def _initial_for_step(step_key: str) -> dict:
    env_map = _STEP_ENV_KEYS.get(step_key, {})
    defaults = _STEP_FIELD_DEFAULTS.get(step_key, {})
    initial = {}

    for field_name, env_key in env_map.items():
        default_value = defaults.get(field_name, "")
        initial[field_name] = _read_env_key(env_key, str(default_value))

    if step_key == WizardProgress.STEP_EMAIL:
        initial["email_port"] = _as_int(initial.get("email_port", "587"), 587)
        initial["email_use_tls"] = _as_bool(initial.get("email_use_tls", "true"))

    if step_key == WizardProgress.STEP_HOSTING:
        initial["whm_port"] = _as_int(initial.get("whm_port", "2087"), 2087)

    if step_key == WizardProgress.STEP_REGISTRAR:
        api_url = (initial.get("resellerclub_api_url") or "").rstrip("/")
        if api_url == RESELLERCLUB_LIVE_API_URL:
            initial["resellerclub_api_mode"] = "live"
        elif api_url == RESELLERCLUB_TEST_API_URL:
            initial["resellerclub_api_mode"] = "test"
        else:
            initial["resellerclub_api_mode"] = "custom"

    return initial


def _test_connection(step_key: str, data: dict):
    """Run lightweight connectivity checks for a wizard step."""
    if step_key == WizardProgress.STEP_REGISTRAR:
        base_url = (data.get("resellerclub_api_url") or "").rstrip("/")
        reseller_id = data.get("resellerclub_reseller_id") or ""
        api_key = data.get("resellerclub_api_key") or ""
        if not (base_url and reseller_id and api_key):
            return False, "Provide API URL, Reseller ID, and API key first."

        # LogicBoxes HTTP API authenticates via query params, NOT Basic Auth
        resp = requests.get(
            f"{base_url}/domains/available",
            params={
                "auth-userid": reseller_id,
                "api-key": api_key,
                "domain-name": "example",
                "tlds": "com",
            },
            timeout=12,
        )
        if resp.status_code >= 400:
            return False, f"ResellerClub HTTP {resp.status_code}: {resp.text[:240]}"
        parsed = resp.json()
        if isinstance(parsed, dict) and parsed.get("status") == "ERROR":
            msg = parsed.get("message") or parsed.get("error") or str(parsed)
            return False, f"ResellerClub error: {msg}"
        return True, f"Connection OK. Sample response keys: {', '.join(list(parsed.keys())[:5])}"

    if step_key == WizardProgress.STEP_HOSTING:
        host = data.get("whm_host") or ""
        port = data.get("whm_port")
        username = data.get("whm_username") or ""
        token = data.get("whm_api_token") or ""
        if not (host and port and username and token):
            return False, "Provide WHM host, port, username, and API token first."

        resp = requests.get(
            f"https://{host}:{port}/json-api/version",
            params={"api.version": 1},
            headers={"Authorization": f"whm {username}:{token}"},
            timeout=12,
        )
        if resp.status_code >= 400:
            return False, f"WHM HTTP {resp.status_code}: {resp.text[:240]}"
        return True, "Connection OK. WHM version endpoint responded successfully."

    if step_key == WizardProgress.STEP_CLOUDFLARE:
        token = data.get("cloudflare_api_token") or ""
        if not token:
            return False, "Provide a Cloudflare API token first."
        resp = requests.get(
            "https://api.cloudflare.com/client/v4/user/tokens/verify",
            headers={"Authorization": f"Bearer {token}"},
            timeout=12,
        )
        if resp.status_code >= 400:
            return False, f"Cloudflare HTTP {resp.status_code}: {resp.text[:240]}"
        parsed = resp.json()
        if not parsed.get("success"):
            return False, f"Cloudflare token not verified: {parsed}"
        return True, "Connection OK. Cloudflare token is valid."

    if step_key == WizardProgress.STEP_PAYMENTS:
        stripe_key = data.get("stripe_secret_key") or ""
        gocardless_token = data.get("gocardless_access_token") or ""
        gc_env = data.get("gocardless_environment") or "sandbox"

        messages = []
        if stripe_key:
            stripe_resp = requests.get(
                "https://api.stripe.com/v1/balance",
                auth=(stripe_key, ""),
                timeout=12,
            )
            if stripe_resp.status_code >= 400:
                return False, f"Stripe HTTP {stripe_resp.status_code}: {stripe_resp.text[:240]}"
            messages.append("Stripe OK")
        else:
            messages.append("Stripe skipped (no key)")

        if gocardless_token:
            base = "https://api.gocardless.com" if gc_env == "live" else "https://api-sandbox.gocardless.com"
            gc_resp = requests.get(
                f"{base}/customers?limit=1",
                headers={
                    "Authorization": f"Bearer {gocardless_token}",
                    "GoCardless-Version": "2015-07-06",
                },
                timeout=12,
            )
            if gc_resp.status_code >= 400:
                return False, f"GoCardless HTTP {gc_resp.status_code}: {gc_resp.text[:240]}"
            messages.append("GoCardless OK")
        else:
            messages.append("GoCardless skipped (no token)")

        return True, ", ".join(messages)

    if step_key == WizardProgress.STEP_EMAIL:
        host = data.get("email_host") or ""
        port = data.get("email_port")
        use_tls = bool(data.get("email_use_tls"))
        username = data.get("email_host_user") or ""
        password = data.get("email_host_password") or ""
        if not (host and port):
            return False, "Provide SMTP host and port first."

        smtp = smtplib.SMTP(host, port, timeout=12)
        try:
            smtp.ehlo()
            if use_tls:
                smtp.starttls()
                smtp.ehlo()
            if username and password:
                smtp.login(username, password)
        finally:
            smtp.quit()
        return True, "Connection OK. SMTP server responded successfully."

    return True, "No connection test is defined for this step."


# ──────────────────────────────────────────────────────────────────────────────
# Views
# ──────────────────────────────────────────────────────────────────────────────

@staff_member_required
def wizard_index(request):
    """Wizard landing page — shows an overview of all steps."""
    if _wizard_disabled():
        messages.info(request, "The setup wizard has been disabled (SETUP_WIZARD_DISABLED=true).")
        return redirect("admin_tools:dashboard")

    progress = WizardProgress.get_or_create_singleton()

    steps = []
    for step_key in WizardProgress.STEPS:
        meta = STEP_META[step_key]
        steps.append({
            "key": step_key,
            "title": meta["title"],
            "description": meta["description"],
            "icon": meta["icon"],
            "done": progress.is_step_done(step_key),
        })

    next_step = progress.next_step()
    return render(request, "admin_tools/wizard/index.html", {
        "steps": steps,
        "next_step": next_step,
        "progress": progress,
        "total": len(WizardProgress.STEPS),
        "done_count": len(progress.completed_steps),
        "all_done": set(WizardProgress.STEPS).issubset(set(progress.completed_steps)),
    })


@staff_member_required
def wizard_step(request, step_key: str):
    """Handle a single wizard step — GET shows the form, POST processes it."""
    if _wizard_disabled():
        return redirect("admin_tools:dashboard")

    if step_key not in WizardProgress.STEPS:
        messages.error(request, f"Unknown setup step: {step_key}")
        return redirect("admin_tools:wizard_index")

    progress = WizardProgress.get_or_create_singleton()
    meta = STEP_META[step_key]
    FormClass = meta["form_class"]
    connection_result = None

    if request.method == "POST":
        action = request.POST.get("action", "save")
        form = FormClass(request.POST)
        if form.is_valid():
            if action == "test":
                ok, detail = _test_connection(step_key, form.cleaned_data)
                connection_result = {"ok": ok, "detail": detail}
                if ok:
                    messages.success(request, f"Connection test passed: {detail}")
                else:
                    messages.error(request, f"Connection test failed: {detail}")
            else:
                _process_step(step_key, form.cleaned_data, request)
                progress.mark_step_done(step_key)

                all_done = set(WizardProgress.STEPS).issubset(set(progress.completed_steps))
                if all_done and not progress.finished:
                    progress.finished = True
                    progress.save(update_fields=["finished"])
                    messages.success(request, "🎉 Setup complete! You can revisit and edit settings at any time.")
                    return redirect("admin_tools:wizard_index")

                next_step = progress.next_step() or step_key
                messages.success(request, f"✓ {meta['title']} saved.")
                return redirect("admin_tools:wizard_step", step_key=next_step)
    else:
        form = FormClass(initial=_initial_for_step(step_key))

    steps = []
    for sk in WizardProgress.STEPS:
        steps.append({
            "key": sk,
            "title": STEP_META[sk]["title"],
            "icon": STEP_META[sk]["icon"],
            "done": progress.is_step_done(sk),
            "current": sk == step_key,
        })

    return render(request, "admin_tools/wizard/step.html", {
        "form": form,
        "meta": meta,
        "step_key": step_key,
        "steps": steps,
        "progress": progress,
        "total": len(WizardProgress.STEPS),
        "done_count": len(progress.completed_steps),
        "connection_result": connection_result,
    })


def _process_step(step_key: str, data: dict, request):
    """Persist the wizard step data — writes .env and performs DB actions."""

    if step_key == WizardProgress.STEP_ADMIN:
        _create_admin_user(data, request)
        return

    env_map = _STEP_ENV_KEYS.get(step_key, {})
    for form_field, env_key in env_map.items():
        value = data.get(form_field)
        if value is not None:
            string_value = str(value)
            _write_env_key(env_key, string_value)
            IntegrationSetting.set_value(
                key=env_key,
                value=string_value,
                is_secret=("KEY" in env_key or "TOKEN" in env_key or "SECRET" in env_key or "PASSWORD" in env_key),
            )


def _create_admin_user(data: dict, request):
    from apps.accounts.models import User
    email = data["email"]
    if User.objects.filter(email__iexact=email).exists():
        messages.warning(request, f"A user with email {email} already exists — skipped creation.")
        return
    User.objects.create_superuser(
        email=email,
        password=data["password"],
        first_name=data.get("first_name", ""),
        last_name=data.get("last_name", ""),
    )
    messages.info(request, f"Admin account created for {email}.")


@staff_member_required
def wizard_reset(request):
    """Reset the wizard progress (allows re-running setup)."""
    if request.method == "POST":
        WizardProgress.objects.all().delete()
        messages.success(request, "Wizard progress reset. You can run setup again.")
        return redirect("admin_tools:wizard_index")
    return render(request, "admin_tools/wizard/reset_confirm.html")
