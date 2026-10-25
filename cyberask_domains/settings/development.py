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

# Local runs may print mail to the console. Production swarm sets
# EMAIL_ALLOW_CONSOLE_FALLBACK=false so a missing SMTP host cannot pretend
# that password resets and customer mail were delivered. Database M365
# settings are read by MicrosoftGraphEmailBackend; do not replace that backend
# here or those settings are ignored.
if "EMAIL_ALLOW_CONSOLE_FALLBACK" not in os.environ:
    EMAIL_ALLOW_CONSOLE_FALLBACK = True  # noqa: F811
