"""Resolve retained registration attempts without repeating a paid call."""

from datetime import timedelta
import re
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.contact_bindings import provider_scope
from apps.domains.models import DomainOrder, DomainRegistrationAttempt
from apps.domains.registration_submission import _identifier, _same_order
from apps.domains.registrar_policy import policy_for
from apps.domains.resellerclub_client import ResellerClubClient


class Command(BaseCommand):
    help = "Release stale preparation or link a strictly matched registrar order. Never registers, pays or sends email."

    def add_arguments(self, parser):
        parser.add_argument("--attempt-id", type=int, required=True)
        parser.add_argument("--owner-id", type=int, required=True)
        parser.add_argument("--action", choices=["release-preparation", "link-order", "adopt-legacy", "confirm-no-order"], required=True)
        parser.add_argument("--remote-order-id")
        parser.add_argument("--customer-id", help="Explicit provider customer ID for read-only historical adoption.")
        parser.add_argument("--provider-reference", required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        reference = options["provider_reference"]
        reason = options["reason"].strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", reference) or not 10 <= len(reason) <= 1000:
            raise CommandError("Supply a provider/review reference and a reason of 10-1000 characters.")
        owner = User.objects.filter(pk=options["owner_id"], is_active=True, is_staff=True, is_superuser=True).first()
        if owner is None:
            raise CommandError("An active owner is required for attribution.")
        attempt = DomainRegistrationAttempt.objects.filter(pk=options["attempt_id"]).first()
        if attempt is None:
            raise CommandError("Registration attempt not found.")
        if any(options[key] is not None and not _identifier(options[key]) for key in ("remote_order_id", "customer_id")):
            raise CommandError("Provider order and customer identifiers must be positive ASCII numeric IDs.")
        if options["action"] in {"adopt-legacy", "confirm-no-order"}:
            from django.core.exceptions import ValidationError
            from apps.domains.historical_registration import review_historical_registration
            try:
                current = review_historical_registration(
                    attempt, owner, action=options["action"], identifier=_identifier(options["remote_order_id"]),
                    customer_id=_identifier(options["customer_id"]), reference=reference, reason=reason,
                    client=ResellerClubClient() if options["action"] == "adopt-legacy" else None,
                )
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from None
            except Exception:
                raise CommandError("Historical review did not complete. Inspect the retained state before retrying.") from None
            self.stdout.write(f"Historical attempt {current.pk}: {current.status}. No purchase, activation or email was performed.")
            return
        if attempt.legacy_review_required or options["customer_id"]:
            raise CommandError("Historical orders require their dedicated owner review; provider customer scope cannot be overridden here.")
        release = options["action"] == "release-preparation"
        identifier = _identifier(options["remote_order_id"])
        if release:
            if options["remote_order_id"] or attempt.status != "preparing" or attempt.submitted_at or attempt.updated_at > timezone.now() - timedelta(minutes=15):
                raise CommandError("Only preparation older than 15 minutes, with no paid-submission marker, can be released.")
        else:
            if not identifier or attempt.status not in {"unknown", "submitting"}:
                raise CommandError("Choose an uncertain submission and a valid registrar order ID.")
            if attempt.status == "submitting" and attempt.submitted_at and attempt.submitted_at > timezone.now() - timedelta(minutes=15):
                raise CommandError("The submission may still be running. Review its worker and wait at least 15 minutes.")
            client = ResellerClubClient()
            if provider_scope(client) != attempt.provider_scope:
                raise CommandError("The registrar environment differs from the retained attempt.")
            try:
                payload = client.get_registrant_verification_details(identifier)
                expected = {"registration_contact": "registrantcontactid", "admin_contact": "admincontactid", "tech_contact": "techcontactid", "billing_contact": "billingcontactid"}
                matched = (
                    isinstance(payload, dict)
                    and _identifier(payload.get("orderid", payload.get("entityid"))) == identifier
                    and payload.get("domainname", "").lower() == attempt.subject["order"]["domain_name"]
                    and _identifier(payload.get("customerid")) == attempt.customer_id
                )
                for role in policy_for(attempt.subject["order"]["tld"]).roles:
                    key = "reg" if role == "registration_contact" else role.removesuffix("_contact")
                    matched = matched and _identifier(payload.get(expected[role])) == attempt.role_ids.get(key)
                if not matched:
                    raise ValueError
            except Exception:
                raise CommandError("The registrar order could not be matched to the retained customer, domain and contacts. No linkage was changed.") from None
        with transaction.atomic():
            order = DomainOrder.objects.select_for_update().get(pk=attempt.order_id)
            current = DomainRegistrationAttempt.objects.select_for_update().get(pk=attempt.pk)
            if current.token != attempt.token or current.status != attempt.status or current.updated_at != attempt.updated_at:
                raise CommandError("The attempt changed during review. Review its current state.")
            if release:
                current.token = uuid.uuid4()  # Fence any delayed original worker.
                current.status = "aborted"
            else:
                if not _same_order(current, order) or order.registrar_order_id and order.registrar_order_id != identifier:
                    raise CommandError("The local order no longer matches the retained submission. Resolve its identity before linking.")
                current.remote_order_id = identifier
                current.status = "accepted"
                order.registrar_order_id = identifier
                order.registrar_contact_ids = current.role_ids
                order.save(update_fields=["registrar_order_id", "registrar_contact_ids", "updated_at"])
            current.completed_at = timezone.now()
            current.save(update_fields=["token", "status", "remote_order_id", "completed_at", "updated_at"])
            AuditLog.objects.create(user=owner, action="domain.registration.reconciled", model_name="DomainOrder", object_id=str(order.pk), data={
                "registration_attempt_id": current.pk, "resolution": options["action"], "provider_reference": reference, "reason": reason,
            })
        self.stdout.write(f"Registration attempt {current.pk}: {current.status}. No purchase, domain activation or email was performed.")
