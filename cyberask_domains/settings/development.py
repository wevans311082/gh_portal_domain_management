from .base import *  # noqa: F401, F403
import os

DEBUG = True

INSTALLED_APPS += ["debug_toolbar"]  # noqa: F405
MIDDLEWARE = ["debug_toolbar.middleware.DebugToolbarMiddleware"] + MIDDLEWARE  # noqa: F405

INTERNAL_IPS = ["127.0.0.1"]

# Avoid browser COOP warning when developing over plain HTTP/non-localhost origins.
SECURE_CROSS_ORIGIN_OPENER_POLICY = None

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Lab convenience: if SMTP host is empty/placeholder, never blow up password reset
# with socket.gaierror — log mail to the web container console instead.
_email_host = (os.environ.get("EMAIL_HOST") or EMAIL_HOST or "").strip()  # noqa: F405
_bad_hosts = {"", "smtp.example.com", "localhost.invalid"}
if _email_host in _bad_hosts and not os.environ.get("M365_GRAPH_ENABLED", "").lower() in ("1", "true", "yes"):
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    M365_GRAPH_FALLBACK_EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
