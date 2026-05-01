"""
Wizard model — stores per-step completion so the wizard can be resumed.
A single SiteConfiguration row is created by the wizard and updated thereafter.
"""
from django.db import models
from apps.core.models import TimeStampedModel


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
