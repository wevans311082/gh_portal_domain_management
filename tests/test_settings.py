from django.conf import settings


def test_celery_beat_and_results_apps_are_installed():
    assert "django_celery_beat" in settings.INSTALLED_APPS
    assert "django_celery_results" in settings.INSTALLED_APPS


def test_pytest_uses_eager_celery_settings():
    assert settings.CELERY_TASK_ALWAYS_EAGER is True
    assert settings.CELERY_TASK_EAGER_PROPAGATES is True


def test_database_scheduler_is_enabled():
    assert settings.CELERY_BEAT_SCHEDULER == "django_celery_beat.schedulers:DatabaseScheduler"


def test_locmem_cache_is_used_in_tests():
    assert settings.CACHES["default"]["BACKEND"] == "django.core.cache.backends.locmem.LocMemCache"