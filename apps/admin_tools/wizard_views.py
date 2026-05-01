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

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, redirect

from .models import WizardProgress

logger = logging.getLogger(__name__)

BASE_DIR = settings.BASE_DIR
_ENV_PATH = BASE_DIR / ".env"


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
    return getattr(settings, key, "") or default


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
    resellerclub_reseller_id = forms.CharField(max_length=50, label="ResellerClub Reseller ID", required=False)
    resellerclub_api_key = forms.CharField(
        max_length=255, label="ResellerClub API Key", widget=forms.PasswordInput, required=False,
    )
    resellerclub_api_url = forms.URLField(
        label="API Base URL",
        initial="https://test.httpapi.com/api",
        help_text="Use https://httpapi.com/api for production.",
        required=False,
    )


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
    if progress.finished:
        messages.success(request, "Setup is already complete!")
        return redirect("admin_tools:dashboard")

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

    if request.method == "POST":
        form = FormClass(request.POST)
        if form.is_valid():
            _process_step(step_key, form.cleaned_data, request)
            progress.mark_step_done(step_key)

            # Check if all steps are done
            if set(WizardProgress.STEPS).issubset(set(progress.completed_steps)):
                progress.finished = True
                progress.save(update_fields=["finished"])
                messages.success(request, "🎉 Setup complete! Your portal is ready.")
                return redirect("admin_tools:dashboard")

            next_step = progress.next_step()
            messages.success(request, f"✓ {meta['title']} saved.")
            return redirect("admin_tools:wizard_step", step_key=next_step)
    else:
        form = FormClass()

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
            _write_env_key(env_key, str(value))


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
