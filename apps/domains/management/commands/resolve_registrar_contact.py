"""Record an operator's provider-side reconciliation; never calls a provider."""

import re
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.models import RegistrarContactBinding


class Command(BaseCommand):
    help = "Associate a reviewed provider contact with a blocked immutable binding; no provider mutation."

    def add_arguments(self, parser):
        parser.add_argument("--binding-id", type=int, required=True)
        parser.add_argument("--owner-id", type=int, required=True)
        parser.add_argument("--remote-id", required=True)
        parser.add_argument("--confirmed-payload-digest", required=True)
        parser.add_argument("--provider-reference", required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        identifier = options["remote_id"]
        reference = options["provider_reference"]
        reason = options["reason"].strip()
        if not re.fullmatch(r"[1-9][0-9]{0,18}", identifier) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", reference) or not 10 <= len(reason) <= 1000:
            raise CommandError("Supply a valid contact ID, provider case reference and review reason (10-1000 characters).")
        owner = User.objects.filter(pk=options["owner_id"], is_active=True, is_staff=True, is_superuser=True).first()
        if owner is None:
            raise CommandError("An active owner account is required for audit attribution.")
        with transaction.atomic():
            binding = RegistrarContactBinding.objects.select_for_update().filter(pk=options["binding_id"]).first()
            if not binding or binding.status not in {"creating", "unknown"}:
                raise CommandError("Select a blocked contact binding; ready bindings cannot be overwritten.")
            if binding.status == "creating" and binding.created_at > timezone.now() - timedelta(minutes=15):
                raise CommandError("Creation may still be running. Stop its worker and allow at least 15 minutes before reconciliation.")
            if binding.payload_digest != options["confirmed_payload_digest"]:
                raise CommandError("The reviewed payload digest does not match this binding.")
            binding.remote_id = identifier
            binding.status = "ready"
            binding.save(update_fields=["remote_id", "status", "updated_at"])
            AuditLog.objects.create(
                user=owner, action="domain.contact.reconciled", model_name="RegistrarContactBinding",
                object_id=str(binding.pk), data={"provider_reference": reference, "reason": reason},
            )
        self.stdout.write(f"Contact binding {binding.pk} reconciled. No domain or verification state was changed.")
