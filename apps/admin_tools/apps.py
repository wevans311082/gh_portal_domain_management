from django.apps import AppConfig


class AdminToolsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.admin_tools"
    label = "admin_tools"

    def ready(self):
        from . import checks  # noqa: F401
        from .search_sqlite import install
        install()
