from django.apps import AppConfig


class ProvisioningConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.provisioning"
    label = "provisioning"

    def ready(self):
        try:
            from apps.provisioning.tasks import ensure_whm_sync_schedule

            ensure_whm_sync_schedule()
        except Exception:
            # Ignore startup ordering issues before migrations create beat tables.
            pass
