import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.models import VerificationAction


class Command(BaseCommand):
    help = "Resolve an uncertain resend after provider review; never sends email or marks a contact verified."

    def add_arguments(self, parser):
        parser.add_argument("--action-id", type=int, required=True)
        parser.add_argument("--owner-id", type=int, required=True)
        parser.add_argument("--outcome", choices=["sent", "not-sent"], required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--provider-reference", required=True)

    def handle(self, *args, **options):
        reason = options["reason"].strip()
        reference = options["provider_reference"].strip()
        if not 10 <= len(reason) <= 1000 or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", reference):
            raise CommandError("Supply a review reason (10-1000 characters) and a provider case reference.")
        owner = User.objects.filter(pk=options["owner_id"], is_active=True, is_staff=True, is_superuser=True).first()
        if owner is None:
            raise CommandError("An active owner account is required for audit attribution.")
        with transaction.atomic():
            job = VerificationAction.objects.select_for_update().filter(pk=options["action_id"]).first()
            if not job or job.status != "unknown" or job.kind != "resend":
                raise CommandError("Select an uncertain resend action; other states cannot be resolved here.")
            job.status = "succeeded" if options["outcome"] == "sent" else "failed"
            job.completed_at = timezone.now()
            job.save(update_fields=["status", "completed_at", "updated_at"])
            AuditLog.objects.create(
                user=owner, action="domain.verification.resolved", model_name="DomainOrder",
                object_id=str(job.domain_order_id), data={
                    "verification_action_id": job.pk, "outcome": options["outcome"],
                    "reason": reason, "provider_reference": reference,
                },
            )
        self.stdout.write(f"Verification action {job.pk} resolved as {job.status}.")
