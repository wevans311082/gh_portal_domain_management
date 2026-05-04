from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"
    label = "billing"

    def ready(self):
        # Register all billing periodic tasks when the app starts.
        # Wrapped in try/except so migrations and test runs don't fail when
        # the django_celery_beat tables don't exist yet.
        try:
            from apps.billing.tasks import ensure_billing_schedules
            ensure_billing_schedules()
        except Exception:
            pass
