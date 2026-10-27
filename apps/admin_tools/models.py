"""
Wizard model — stores per-step completion so the wizard can be resumed.
Integration settings can be persisted in DB for runtime updates without
editing environment files.
"""
from django.db import models, transaction
from django.core.cache import cache
from apps.core.models import TimeStampedModel


class IntegrationSettingQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if {"key", "value", "is_secret"}.intersection(kwargs):
            from .secrets import IntegrationSecretError
            raise IntegrationSecretError("Use IntegrationSetting.set_value for protected settings writes.")
        return super().update(**kwargs)

    def bulk_create(self, *args, **kwargs):
        from .secrets import IntegrationSecretError
        raise IntegrationSecretError("Use IntegrationSetting.set_value for protected settings writes.")

    def bulk_update(self, *args, **kwargs):
        from .secrets import IntegrationSecretError
        raise IntegrationSecretError("Use IntegrationSetting.set_value for protected settings writes.")


class IntegrationSetting(TimeStampedModel):
    """Key/value settings persisted in the database for runtime integrations."""

    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True, default="")
    is_secret = models.BooleanField(default=True)
    objects = IntegrationSettingQuerySet.as_manager()

    class Meta:
        ordering = ["key"]
        verbose_name = "Integration setting"
        verbose_name_plural = "Integration settings"
        permissions = (
            ("view_customers", "View customer accounts"),
            ("view_billing", "View invoices, quotes and payments"),
            ("manage_billing", "Manage invoices, quotes and payments"),
            ("view_domains", "View domains and orders"),
            ("manage_domains", "Manage domains and orders"),
            ("view_services", "View hosting services"),
            ("manage_services", "Manage hosting services"),
            ("view_support", "View support tickets and contact enquiries"),
            ("manage_support", "Manage support tickets and contact enquiries"),
            ("view_content", "View website content"),
            ("manage_content", "Manage website content"),
            ("view_audit", "View application audit records"),
        )

    def __str__(self):
        return self.key

    @property
    def plaintext_value(self):
        from .secrets import decrypt_value
        return decrypt_value(self.key, self.value)

    def save(self, *args, **kwargs):
        from .secrets import PREFIX, decrypt_value, encrypt_value
        if self.value.startswith(PREFIX):
            # Reject copied, corrupt or unknown-key envelopes before persisting.
            decrypt_value(self.key, self.value)
        else:
            self.value = encrypt_value(self.key, self.value)
        super().save(*args, **kwargs)
        cache.delete(f"runtime_setting:{self.key}")

    @classmethod
    def get_value(cls, key: str, default: str = "") -> str:
        item = cls.objects.filter(key=key).first()
        return item.plaintext_value if item is not None else default

    @classmethod
    @transaction.atomic
    def set_value(cls, key: str, value: str, is_secret: bool = True, actor=None):
        obj = cls.objects.update_or_create(
            key=key,
            defaults={"value": value, "is_secret": is_secret},
        )
        cache.delete(f"runtime_setting:{key}")
        from apps.audit.models import AuditLog
        AuditLog.objects.create(user=actor, action="integration.setting.updated", model_name="admin_tools.IntegrationSetting",
                                object_id=str(obj[0].pk), data={"key": key, "is_secret": is_secret, "created": obj[1]})
        return obj


class WizardProgress(TimeStampedModel):
    """Tracks which setup wizard steps have been completed."""

    STEP_SITE = "site"
    STEP_ADMIN = "admin"
    STEP_EMAIL = "email"
    STEP_PAYMENTS = "payments"
    STEP_REGISTRAR = "registrar"
    STEP_HOSTING = "hosting"
    STEP_CLOUDFLARE = "cloudflare"

    STEPS = [
        STEP_SITE,
        STEP_ADMIN,
        STEP_EMAIL,
        STEP_PAYMENTS,
        STEP_REGISTRAR,
        STEP_HOSTING,
        STEP_CLOUDFLARE,
    ]

    completed_steps = models.JSONField(default=list)
    finished = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Wizard Progress"

    def is_step_done(self, step: str) -> bool:
        return step in self.completed_steps

    def mark_step_done(self, step: str):
        if step not in self.completed_steps:
            self.completed_steps.append(step)
        self.save(update_fields=["completed_steps"])

    def next_step(self) -> str | None:
        for step in self.STEPS:
            if step not in self.completed_steps:
                return step
        return None

    @classmethod
    def get_or_create_singleton(cls):
        obj = cls.objects.first()
        if not obj:
            obj = cls.objects.create()
        return obj
