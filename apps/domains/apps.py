from django.apps import AppConfig


class DomainsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.domains"
    label = "domains"

    def ready(self):
        try:
            from apps.domains.tasks import ensure_auto_renew_schedule, ensure_registrar_balance_schedule
            ensure_auto_renew_schedule()
            ensure_registrar_balance_schedule()
        except Exception:
            pass
