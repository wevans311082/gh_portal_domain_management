"""Resolve a transfer without ever repeating the paid transfer POST."""

from datetime import timedelta
import re
import uuid

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.contact_bindings import provider_scope
from apps.domains.historical_registration import verified_existing_order
from apps.domains.models import DomainTransfer, DomainTransferAttempt
from apps.domains.registration_submission import _identifier
from apps.domains.resellerclub_client import ResellerClubClient
from apps.domains.transfer_submission import _identity
from apps.domains.registrar_policy import ALL_ROLES, policy_for


class Command(BaseCommand):
    help = "Audited transfer recovery. Never purchases, charges, activates or sends email."

    def add_arguments(self, parser):
        parser.add_argument("--attempt-id", type=int, required=True)
        parser.add_argument("--owner-id", type=int, required=True)
        parser.add_argument("--action", required=True, choices=["release-preparation", "link-order", "adopt-legacy", "confirm-no-order"])
        parser.add_argument("--remote-order-id")
        parser.add_argument("--customer-id")
        parser.add_argument("--provider-reference", required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        reference, reason = options["provider_reference"], options["reason"].strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", reference) or not 10 <= len(reason) <= 1000:
            raise CommandError("Supply a provider case/reference and a reason of 10-1000 characters.")
        if not User.objects.filter(pk=options["owner_id"], is_active=True, is_staff=True, is_superuser=True).exists():
            raise CommandError("An active owner is required for attribution.")
        attempt = DomainTransferAttempt.objects.select_related("transfer").filter(pk=options["attempt_id"]).first()
        if attempt is None:
            raise CommandError("Transfer attempt not found.")
        for key in ("remote_order_id", "customer_id"):
            if options[key] is not None and not _identifier(options[key]):
                raise CommandError("Provider identifiers must be positive ASCII numeric IDs.")
        action = options["action"]
        legacy = action in {"adopt-legacy", "confirm-no-order"}
        if legacy != attempt.legacy_review_required or (options["customer_id"] is not None and action != "adopt-legacy"):
            raise CommandError("Use the matching historical or modern recovery action; customer scope cannot be overridden.")
        identifier = _identifier(options["remote_order_id"])
        facts, scope, customer_id = None, attempt.provider_scope, attempt.customer_id
        snapshot = _identity(attempt.transfer)
        try:
            if action == "release-preparation":
                if (identifier or attempt.status != "preparing" or attempt.submitted_at
                        or attempt.updated_at > timezone.now() - timedelta(minutes=15)):
                    raise CommandError("Only preparation older than 15 minutes without a paid marker can be released.")
            elif action == "confirm-no-order":
                if identifier or attempt.status != "unknown" or _identifier(attempt.transfer.registrar_order_id) or attempt.transfer.domain_id:
                    raise CommandError("Only an unlinked historical blank/invalid-ID request can record no existing or pending order.")
            else:
                if not identifier or attempt.status not in {"unknown", "submitting"}:
                    raise CommandError("Choose an uncertain transfer and a valid provider order ID.")
                if attempt.status == "submitting" and attempt.submitted_at and attempt.submitted_at > timezone.now() - timedelta(minutes=15):
                    raise CommandError("The paid submission may still be running. Stop/review its worker and wait at least 15 minutes.")
                if policy_for(attempt.transfer.tld).roles != ALL_ROLES:
                    raise CommandError("This extension needs its registrar-specific transfer workflow.")
                customer_id = _identifier(options["customer_id"]) if legacy else attempt.customer_id
                if not customer_id:
                    raise CommandError("Supply the provider customer ID for historical adoption.")
                client = ResellerClubClient()
                scope = provider_scope(client)
                if not legacy and scope != attempt.provider_scope:
                    raise CommandError("The registrar account differs from the retained attempt.")
                try:
                    payload = client.get_registration_reconciliation_details(identifier)
                    facts = verified_existing_order(payload, identifier=identifier,
                        domain_name=attempt.transfer.domain_name, customer_id=customer_id)
                except Exception:
                    raise CommandError("The provider order identity and facts could not be confirmed. No recovery was applied.") from None
                if not legacy and any(facts["role_ids"][key] != attempt.role_ids.get(key) for key in ("reg", "admin", "tech", "billing")):
                    raise CommandError("Provider contact roles do not match the retained paid submission.")
            with transaction.atomic():
                current = DomainTransfer.objects.select_for_update().get(pk=attempt.transfer_id)
                retained = DomainTransferAttempt.objects.select_for_update().get(pk=attempt.pk)
                owner = User.objects.select_for_update().filter(pk=options["owner_id"], is_active=True, is_staff=True, is_superuser=True).first()
                if owner is None or retained.token != attempt.token or retained.updated_at != attempt.updated_at or _identity(current) != snapshot:
                    raise CommandError("Owner access or transfer state changed during review. No recovery was applied.")
                if current.domain_id != attempt.transfer.domain_id or current.registrar_order_id != attempt.transfer.registrar_order_id:
                    raise CommandError("The retained local association changed during review.")
                if action in {"release-preparation", "confirm-no-order"}:
                    retained.status, retained.token = "aborted", uuid.uuid4()
                    retained.legacy_review_required = False
                    if legacy:
                        current.registrar_order_id = ""
                        current.registrar_contact_ids = {}
                        current.save(update_fields=["registrar_order_id", "registrar_contact_ids", "updated_at"])
                else:
                    if current.registrar_order_id and _identifier(current.registrar_order_id) and current.registrar_order_id != identifier:
                        raise CommandError("A different known registrar order cannot be overwritten.")
                    if current.domain_id and (current.domain.user_id != current.user_id or current.domain.name != current.domain_name or current.domain.registrar_id != identifier):
                        raise CommandError("The linked domain conflicts with the reviewed transfer order.")
                    if legacy:
                        retained.subject = snapshot
                        retained.subject["nameservers"] = facts["nameservers"]
                        retained.subject["reviewed_registry"] = {"status": facts["status"], "checked_at": facts["checked_at"].isoformat(),
                            "registered_at": facts["registered_at"].isoformat() if facts["registered_at"] else None,
                            "expires_at": facts["expires_at"].isoformat() if facts["expires_at"] else None}
                        retained.role_ids = {**facts["role_ids"], "_scope": scope}
                    elif any(snapshot[key] != retained.subject.get(key) for key in snapshot) or current.registrar_contact_ids != retained.role_ids:
                        raise CommandError("The paid transfer identity or role snapshot changed. Review it manually.")
                    retained.status, retained.remote_order_id = "accepted", identifier
                    retained.provider_scope, retained.customer_id = scope, customer_id
                    retained.legacy_review_required = False
                    current.registrar_order_id, current.registrar_contact_ids = identifier, retained.role_ids
                    current.save(update_fields=["registrar_order_id", "registrar_contact_ids", "updated_at"])
                retained.completed_at = timezone.now()
                retained.save()
                AuditLog.objects.create(user=owner, action="domain.transfer.reconciled", model_name="DomainTransfer", object_id=str(current.pk),
                    data={"transfer_attempt_id": retained.pk, "resolution": action, "provider_reference": reference, "reason": reason})
        except (CommandError, ValidationError) as exc:
            raise CommandError(" ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)) from None
        except Exception:
            raise CommandError("Transfer review did not complete. Inspect the retained state before retrying.") from None
        self.stdout.write(f"Transfer attempt {attempt.pk}: {retained.status}. No purchase, activation or email was performed.")
