"""Explicit, bounded schema for portable application configuration backups."""
from decimal import Decimal, InvalidOperation
from ipaddress import ip_address
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.core.validators import validate_email


class SettingValidationError(ValueError):
    pass


# (kind, secret, maximum length); bootstrap/process/security settings such as
# SECRET_KEY, database URLs, proxy trust and encryption keys are never portable.
SETTING_SCHEMA = {}


def _add(names, kind="text", secret=False, limit=4096):
    for name in names.split():
        SETTING_SCHEMA[name] = (kind, secret, limit)


_add("SITE_NAME", limit=100)
_add("SITE_DOMAIN EMAIL_HOST WHM_HOST", "host", limit=253)
_add("DJANGO_TIME_ZONE", "timezone", limit=100)
_add("DJANGO_ADMIN_URL", "slug", limit=80)
_add("EMAIL_HOST_USER", limit=255)
_add("WHM_USERNAME", limit=100)
_add("EMAIL_HOST_PASSWORD STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET GOCARDLESS_ACCESS_TOKEN "
     "GOCARDLESS_WEBHOOK_SECRET RESELLERCLUB_API_KEY WHM_API_TOKEN CLOUDFLARE_API_TOKEN "
     "M365_GRAPH_CLIENT_SECRET COMPANIES_HOUSE_API_KEY", secret=True)
_add("STRIPE_PUBLISHABLE_KEY M365_GRAPH_TENANT_ID M365_GRAPH_CLIENT_ID", limit=255)
_add("EMAIL_PORT WHM_PORT", "port", limit=5)
_add("EMAIL_USE_TLS M365_GRAPH_ENABLED M365_GRAPH_SAVE_TO_SENT_ITEMS RESELLERCLUB_DEBUG_MODE "
     "FEATURE_DOMAIN_TRANSFERS FEATURE_BULK_DOMAIN_RENEW FEATURE_ACCOUNT_STATEMENT "
     "FEATURE_NOTIFICATION_PREFS FEATURE_BLOG FEATURE_TESTIMONIALS", "boolean", limit=5)
_add("DEFAULT_FROM_EMAIL CLOUDFLARE_EMAIL M365_GRAPH_DEFAULT_MAILBOX M365_GRAPH_BILLING_MAILBOX "
     "M365_GRAPH_SUPPORT_MAILBOX M365_GRAPH_DOMAINS_MAILBOX M365_GRAPH_NOTIFICATIONS_MAILBOX", "email", limit=254)
_add("GOCARDLESS_ENVIRONMENT", "environment", limit=7)
_add("RESELLERCLUB_RESELLER_ID RESELLERCLUB_CUSTOMER_ID", "identifier", limit=50)
_add("CLOUDFLARE_ACCOUNT_ID", "cloudflare_account", limit=32)
_add("RESELLERCLUB_API_URL", "url", limit=2048)
_add("WHM_NAMESERVERS", "hosts", limit=3400)
_add("WHM_DNS_ZONE_IP", "ipv4", limit=15)
_add("REGISTRAR_LOW_BALANCE_THRESHOLD", "money", limit=20)
_add("M365_GRAPH_TIMEOUT_SECONDS", "timeout", limit=3)


def _hostname(value):
    try:
        ip_address(value)
        return
    except ValueError:
        pass
    encoded = value.encode("idna").decode("ascii")
    if len(encoded) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                 for label in encoded.rstrip(".").split(".")):
        raise ValueError


def validate_setting(key, value):
    if not isinstance(key, str) or key not in SETTING_SCHEMA:
        raise SettingValidationError("Backup contains an unsupported setting key.")
    kind, secret, limit = SETTING_SCHEMA[key]
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SettingValidationError(f"Backup value for {key} has an invalid type or length.")
    try:
        value.encode("utf-8")
        if key in {"SITE_NAME", "SITE_DOMAIN"} and not value.strip():
            raise ValueError
        if value == "":
            if kind in {"boolean", "port", "timeout", "environment", "timezone", "slug"} or key == "SITE_NAME":
                raise ValueError
            return value, secret
        if kind == "boolean":
            if value.lower() not in {"true", "false"}:
                raise ValueError
            value = value.lower()
        elif kind in {"port", "timeout"}:
            if not value.isascii() or not value.isdecimal() or not (1 <= int(value) <= 65535 if kind == "port" else 3 <= int(value) <= 120):
                raise ValueError
            value = str(int(value))
        elif kind == "host":
            _hostname(value)
        elif kind == "ipv4":
            parsed_ip = ip_address(value)
            if parsed_ip.version != 4:
                raise ValueError
            value = str(parsed_ip)
        elif kind == "hosts":
            hosts = value.split(",")
            if not 1 <= len(hosts) <= 13 or len(set(host.strip().lower() for host in hosts)) != len(hosts):
                raise ValueError
            for host in hosts:
                _hostname(host.strip())
        elif kind == "email":
            validate_email(value)
        elif kind == "environment" and value not in {"sandbox", "live"}:
            raise ValueError
        elif kind == "timezone":
            ZoneInfo(value)
        elif kind == "slug" and not re.fullmatch(r"[A-Za-z0-9_-]+/", value):
            raise ValueError
        elif kind == "identifier" and (not value.isascii() or not value.isdecimal()):
            raise ValueError
        elif kind == "cloudflare_account" and not re.fullmatch(r"[a-fA-F0-9]{32}", value):
            raise ValueError
        elif kind == "url":
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError
            _hostname(parsed.hostname)
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
        elif kind == "money":
            amount = Decimal(value)
            if not amount.is_finite() or not 0 <= amount <= 1_000_000 or amount.as_tuple().exponent < -2:
                raise ValueError
    except (ValueError, UnicodeError, InvalidOperation, ValidationError, ZoneInfoNotFoundError):
        raise SettingValidationError(f"Backup value for {key} is invalid.") from None
    return value, secret
