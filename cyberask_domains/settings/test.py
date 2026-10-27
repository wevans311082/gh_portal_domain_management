import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from .base import *  # noqa: F401, F403

DEBUG = False

# Public deterministic fixture key, restricted to isolated test settings.
INTEGRATION_ENCRYPTION_KEYS = {"test-v1": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}
INTEGRATION_ENCRYPTION_ACTIVE_KEY = "test-v1"

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
    "public_abuse": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "public-abuse-tests",
    },
}
PUBLIC_ABUSE_CACHE_ALIAS = "public_abuse"

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

RATELIMIT_ENABLE = False
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"

# Authentication backends intentionally inherit the normal application settings.
# Security tests must exercise the same authentication policy used outside tests.
