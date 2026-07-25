import os
from pathlib import Path
import environ

env = environ.Env()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

environ.Env.read_env(os.path.join(BASE_DIR, ".env"))

SECRET_KEY = env("DJANGO_SECRET_KEY")

DEBUG = env.bool("DJANGO_DEBUG", default=False)

ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS",
    default=[
        "localhost",
        "127.0.0.1",
        "10.0.0.46",
        "www.cyberask.co.uk",
        "portal.cyberask.co.uk",
        "billing.cyberask.co.uk",
        "domains.cyberask.co.uk",
        "cyberask.co.uk",
    ],
)

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.contrib.sites",
]

THIRD_PARTY_APPS = [
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "django_celery_beat",
    "django_celery_results",
    "django_extensions",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.portal",
    "apps.products",
    "apps.services",
    "apps.billing",
    "apps.invoices",
    "apps.payments",
    "apps.provisioning",
    "apps.domains",
    "apps.dns",
    "apps.cloudflare_integration",
    "apps.support",
    "apps.companies",
    "apps.notifications",
    "apps.audit",
    "apps.admin_tools",
    "apps.website_templates",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "apps.core.middleware.SubdomainURLRoutingMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    # Custom middleware
    "apps.core.middleware.ResellerClubDebugCaptureMiddleware",
    "apps.core.middleware.RequestCorrelationIDMiddleware",
    "apps.core.middleware.ContentSecurityPolicyMiddleware",
    "apps.audit.middleware.AuditLogMiddleware",
    "apps.audit.middleware.IPAllowlistMiddleware",
]

ROOT_URLCONF = "cyberask_domains.urls"
HOST_URLCONF_MAP = {
    "portal.cyberask.co.uk": "cyberask_domains.urls_portal",
    "billing.cyberask.co.uk": "cyberask_domains.urls_billing",
    "domains.cyberask.co.uk": "cyberask_domains.urls_domains",
}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site_settings",
                "apps.core.context_processors.announcement_banners",
            ],
        },
    },
]

WSGI_APPLICATION = "cyberask_domains.wsgi.application"

DATABASES = {
    "default": env.db("DATABASE_URL", default="sqlite:///db.sqlite3")
}

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env("REDIS_URL", default="redis://127.0.0.1:6379/0"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    "apps.accounts.backends.EmailBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# Session hardening
SESSION_COOKIE_AGE = 60 * 60 * 8  # 8 hours
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_HTTPONLY = True

# Security headers
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# Login rate limiting — max 5 failures per 5 minutes per IP
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = env.int("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", default=5)
LOGIN_RATE_LIMIT_WINDOW_SECONDS = env.int("LOGIN_RATE_LIMIT_WINDOW_SECONDS", default=300)

# Content Security Policy (additional sources can be added here via env)
CSP_DEFAULT_SRC = ["'self'"]
CSP_SCRIPT_SRC = ["'self'"]
CSP_STYLE_SRC = ["'self'", "'unsafe-inline'"]
CSP_IMG_SRC = ["'self'", "data:"]
CSP_FONT_SRC = ["'self'", "data:"]
CSP_CONNECT_SRC = ["'self'"]
CSP_FRAME_ANCESTORS = ["'none'"]

SITE_ID = 1

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://127.0.0.1:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "Europe/London"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_RESULT_EXPIRES = 60 * 60 * 24 * 7  # 7 days — prevents unbounded Redis growth
CELERY_RESULT_EXTENDED = True
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_IMPORTS = ("apps.provisioning.tasks",)

# ---------------------------------------------------------------------------
# Website Templates
# ---------------------------------------------------------------------------
import os as _os

WEBSITE_TEMPLATES_ZIP_ROOT = env(
    "WEBSITE_TEMPLATES_ZIP_ROOT",
    default=str(BASE_DIR / "website_templates" / "Website Templates"),
)
WEBSITE_TEMPLATES_EXTRACTED_ROOT = env(
    "WEBSITE_TEMPLATES_EXTRACTED_ROOT",
    default=str(BASE_DIR / "website_templates" / "extracted"),
)

EMAIL_BACKEND = env("EMAIL_BACKEND", default="apps.notifications.email_backend.MicrosoftGraphEmailBackend")
# Prefer console when SMTP is not configured (avoids socket.gaierror on password reset).
M365_GRAPH_FALLBACK_EMAIL_BACKEND = env(
    "M365_GRAPH_FALLBACK_EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
M365_GRAPH_ENABLED = env.bool("M365_GRAPH_ENABLED", default=False)
M365_GRAPH_TENANT_ID = env("M365_GRAPH_TENANT_ID", default="")
M365_GRAPH_CLIENT_ID = env("M365_GRAPH_CLIENT_ID", default="")
M365_GRAPH_CLIENT_SECRET = env("M365_GRAPH_CLIENT_SECRET", default="")
M365_GRAPH_DEFAULT_MAILBOX = env("M365_GRAPH_DEFAULT_MAILBOX", default="")
M365_GRAPH_BILLING_MAILBOX = env("M365_GRAPH_BILLING_MAILBOX", default="")
M365_GRAPH_SUPPORT_MAILBOX = env("M365_GRAPH_SUPPORT_MAILBOX", default="")
M365_GRAPH_DOMAINS_MAILBOX = env("M365_GRAPH_DOMAINS_MAILBOX", default="")
M365_GRAPH_NOTIFICATIONS_MAILBOX = env("M365_GRAPH_NOTIFICATIONS_MAILBOX", default="")
M365_GRAPH_SAVE_TO_SENT_ITEMS = env.bool("M365_GRAPH_SAVE_TO_SENT_ITEMS", default=True)
M365_GRAPH_TIMEOUT_SECONDS = env.int("M365_GRAPH_TIMEOUT_SECONDS", default=15)
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Cyber Ask Domains <noreply@cyberask.co.uk>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_AUTHENTICATION_METHOD = "email"
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/portal/"
LOGOUT_REDIRECT_URL = "/"
# Empty string disables domain-scoped cookies so IP + hostname both work in lab.
_session_cookie_domain = env("SESSION_COOKIE_DOMAIN", default="")
SESSION_COOKIE_DOMAIN = _session_cookie_domain or None
_csrf_cookie_domain = env("CSRF_COOKIE_DOMAIN", default="")
CSRF_COOKIE_DOMAIN = _csrf_cookie_domain or None
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=[
        "http://10.0.0.46",
        "http://localhost",
        "http://127.0.0.1",
        "https://www.cyberask.co.uk",
        "https://portal.cyberask.co.uk",
        "https://billing.cyberask.co.uk",
        "https://domains.cyberask.co.uk",
        "https://cyberask.co.uk",
    ],
)

SITE_DOMAIN = env("SITE_DOMAIN", default="www.cyberask.co.uk")
SITE_NAME = env("SITE_NAME", default="Cyber Ask Domains")
# Randomise the admin URL — set this to a secret slug in production
DJANGO_ADMIN_URL = env("DJANGO_ADMIN_URL", default="manage-site-a3f7c2/")

STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")

GOCARDLESS_ACCESS_TOKEN = env("GOCARDLESS_ACCESS_TOKEN", default="")
GOCARDLESS_ENVIRONMENT = env("GOCARDLESS_ENVIRONMENT", default="sandbox")
GOCARDLESS_WEBHOOK_SECRET = env("GOCARDLESS_WEBHOOK_SECRET", default="")

PAYPAL_CLIENT_ID = env("PAYPAL_CLIENT_ID", default="")
PAYPAL_CLIENT_SECRET = env("PAYPAL_CLIENT_SECRET", default="")
PAYPAL_MODE = env("PAYPAL_MODE", default="sandbox")

WHM_HOST = env("WHM_HOST", default="")
WHM_PORT = env.int("WHM_PORT", default=2087)
WHM_USERNAME = env("WHM_USERNAME", default="root")
WHM_API_TOKEN = env("WHM_API_TOKEN", default="")

RESELLERCLUB_RESELLER_ID = env("RESELLERCLUB_RESELLER_ID", default="")
RESELLERCLUB_CUSTOMER_ID = env("RESELLERCLUB_CUSTOMER_ID", default="")
RESELLERCLUB_API_KEY = env("RESELLERCLUB_API_KEY", default="")
RESELLERCLUB_API_URL = env("RESELLERCLUB_API_URL", default="https://httpapi.com/api")

CLOUDFLARE_API_TOKEN = env("CLOUDFLARE_API_TOKEN", default="")
CLOUDFLARE_EMAIL = env("CLOUDFLARE_EMAIL", default="")
PLATFORM_WWW_TARGET = env("PLATFORM_WWW_TARGET", default="")
WHM_NAMESERVERS = env.list("WHM_NAMESERVERS", default=[])

COMPANIES_HOUSE_API_KEY = env("COMPANIES_HOUSE_API_KEY", default="")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}
