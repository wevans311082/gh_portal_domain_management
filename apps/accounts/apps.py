from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"

    def ready(self):
        from . import checks  # noqa: F401 - register authentication policy checks
        from allauth.account.signals import password_reset

        from apps.accounts.verification import send_validation_after_password_reset

        password_reset.connect(
            send_validation_after_password_reset,
            dispatch_uid="accounts_validation_after_password_reset",
        )
