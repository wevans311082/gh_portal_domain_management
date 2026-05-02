"""
Wizard model — stores per-step completion so the wizard can be resumed.
Integration settings can be persisted in DB for runtime updates without
editing environment files.
"""
from django.db import models
from django.core.cache import cache
from apps.core.models import TimeStampedModel


class IntegrationSetting(TimeStampedModel):
    """Key/value settings persisted in the database for runtime integrations."""

    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True, default="")
    is_secret = models.BooleanField(default=True)

    class Meta:
        ordering = ["key"]
        verbose_name = "Integration setting"
        verbose_name_plural = "Integration settings"

    def __str__(self):
        return self.key

    @classmethod
    def get_value(cls, key: str, default: str = "") -> str:
        try:
            value = cls.objects.filter(key=key).values_list("value", flat=True).first()
            return value if value not in (None, "") else default
        except Exception:
            return default

    @classmethod
    def set_value(cls, key: str, value: str, is_secret: bool = True):
        obj = cls.objects.update_or_create(
            key=key,
            defaults={"value": value, "is_secret": is_secret},
        )
        cache.delete(f"runtime_setting:{key}")
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
