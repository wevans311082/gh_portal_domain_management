"""Owner-reviewed resolution of public outbox failures without blind resends."""
from django import forms
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.core.models import PublicDelivery
from apps.core.public_submissions import publish_delivery
from .access import is_owner, recent_mfa


class PublicDeliveryReviewForm(forms.Form):
    job = forms.UUIDField(label="Delivery reference")
    action = forms.ChoiceField(choices=[
        ("retry_preparation", "Retry failed preparation"),
        ("confirm_sent", "Confirm delivery from provider evidence"),
        ("cancel", "Close without another send"),
    ])
    reason = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"rows": 2}), label="Reason / evidence")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "w-full rounded-lg border border-slate-400 bg-white px-3 py-2 text-slate-900"

    def full_clean(self):
        super().full_clean()
        for name in self.errors:
            if name in self.fields:
                self.fields[name].widget.attrs.update({"aria-invalid": "true", "aria-describedby": f"id_{name}_error"})


def review_delivery(request, form):
    if not is_owner(request.user) or not recent_mfa(request):
        raise PermissionDenied("A recently verified platform owner is required.")
    with transaction.atomic():
        job = PublicDelivery.objects.select_for_update().filter(pk=form.cleaned_data["job"]).first()
        if job is None:
            form.add_error("job", "This delivery reference does not exist.")
            return False
        action, before = form.cleaned_data["action"], job.state
        if action == "retry_preparation" and before == "failed":
            job.state, job.attempts, job.error_code = "queued", 0, ""
            job.available_at = timezone.now()
        elif action == "confirm_sent" and before == "unknown":
            job.state, job.data, job.error_code = "sent", {}, "manually_confirmed"
            job.completed_at = timezone.now()
        elif action == "cancel" and before in {"unknown", "failed", "queued"}:
            job.state, job.data, job.error_code = "cancelled", {}, "manually_closed"
            job.completed_at = timezone.now()
        else:
            form.add_error("action", "This action is not allowed for the delivery's current state. Refresh and review its outcome.")
            return False
        job.lease_token = None
        job.save(update_fields=["state", "data", "error_code", "completed_at", "available_at", "attempts", "lease_token", "updated_at"])
        AuditLog.objects.create(user=request.user, action="public.delivery.reviewed", model_name="core.PublicDelivery",
                                object_id=str(job.pk), data={"before": before, "after": job.state, "action": action,
                                                            "reason": form.cleaned_data["reason"], "total_attempts": job.total_attempts})
        if job.state == "queued":
            transaction.on_commit(lambda identifier=job.pk: publish_delivery(identifier))
    return True
