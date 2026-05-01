from django.apps import AppConfig


class DomainsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.domains"
    label = "domains"

    def ready(self):
        # Register the daily auto-renew Beat task when the app starts.
        # Wrapped in a try/except so migrations and test runs don't fail when
        # the django_celery_beat tables don't exist yet.
        try:
            from apps.domains.tasks import ensure_auto_renew_schedule
            ensure_auto_renew_schedule()
        except Exception:
            pass
