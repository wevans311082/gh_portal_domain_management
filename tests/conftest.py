import os
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cyberask_domains.settings.test")
os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key")


@pytest.fixture(autouse=True)
def isolate_public_abuse_counters():
    from django.core.cache import caches
    from django.conf import settings
    if settings.PUBLIC_ABUSE_CACHE_ALIAS != "public_abuse" or settings.CACHES["public_abuse"]["BACKEND"] != "django.core.cache.backends.locmem.LocMemCache":
        pytest.fail("Public abuse tests require the isolated test cache; refusing to clear a shared cache.")
    backend = caches[settings.PUBLIC_ABUSE_CACHE_ALIAS]
    backend.clear()
    yield
    backend.clear()
