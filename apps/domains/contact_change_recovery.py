"""Audited owner recovery that never repeats a contact-change mutation."""

from datetime import timedelta
import re
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.contact_bindings import provider_scope
from apps.domains.contact_change_confirmation import ContactChangeAction, identifier
from apps.domains.contact_changes import locked_source
from apps.domains.models import Domain, DomainContactChange


def recover_contact_change(change_id, *, owner_id, action, action_id="", reference, reason, client=None):
    if (not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", reference)
            or not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 1000
            or action not in {"link-action", "cancel-preparation"}):
        raise ValidationError("Supply a recovery action, provider case reference and reason of 10-1000 characters.")
    if not User.objects.filter(pk=owner_id, is_active=True, is_staff=True, is_superuser=True).exists():
        raise ValidationError("An active owner is required for recovery attribution.")
    original = DomainContactChange.objects.get(pk=change_id)
    cutoff = timezone.now() - timedelta(minutes=15)
    if original.updated_at > cutoff or original.lease_until and original.lease_until > timezone.now():
        raise ValidationError("Stop/review the worker and wait at least 15 minutes after its last update before recovery.")
    evidence = None
    if action == "link-action":
        if (not identifier(action_id) or client is None or original.status not in {"unknown", "submitting"}
                or original.submitted_at is None or not original.role_ids
                or original.action_id and original.action_id != identifier(action_id)):
            raise ValidationError("Choose an uncertain submitted request and its exact registrar action ID.")
        if provider_scope(client) != original.provider_scope:
            raise ValidationError("The registrar environment differs from the captured request.")
        evidence = client.get_contact_change_action(original.registrar_order_id, identifier(action_id))
        if (not isinstance(evidence, ContactChangeAction) or evidence.order_id != original.registrar_order_id
                or evidence.action_id != identifier(action_id)):
            raise ValidationError("The registrar action could not be matched. No recovery was applied.")
    elif (action_id or original.submitted_at is not None
            or original.status not in {"preparing", "unknown", "failed"}):
        raise ValidationError("Only preparation without a submission marker may be cancelled through this action.")

    with transaction.atomic(durable=True):
        # Follow admission's customer/domain/source/request lock order.
        User.objects.select_for_update().get(pk=original.user_id)
        domain = Domain.objects.select_for_update().get(pk=original.domain_id)
        source, attempt, kind = locked_source(domain)
        current = DomainContactChange.objects.select_for_update().get(pk=change_id)
        actor = User.objects.select_for_update().filter(pk=owner_id, is_active=True, is_staff=True, is_superuser=True).first()
        if (actor is None or current.token != original.token or current.updated_at != original.updated_at
                or current.status != original.status or current.subject != original.subject or current.role_ids != original.role_ids
                or current.submitted_at != original.submitted_at or current.action_id != original.action_id
                or domain.user_id != current.user_id or domain.name != current.domain_name
                or domain.registrar_id != current.registrar_order_id or source.pk != current.subject.get("source_id")
                or kind != current.subject.get("source_kind") or attempt.customer_id != current.customer_id
                or attempt.provider_scope != current.provider_scope
                or source.registrar_contact_ids != current.subject.get("previous_remote_ids")):
            raise ValidationError("Owner access or the retained association changed during recovery.")
        current.token = uuid.uuid4()  # Fence any surviving preparation worker.
        current.lease_until = None
        current.status = "awaiting" if evidence else "cancelled"
        if evidence:
            current.action_id = evidence.action_id
        current.available_at = timezone.now()
        current.save(update_fields=["token", "lease_until", "status", "action_id", "available_at", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.contact_change." + action.replace("-", "_"),
            model_name="Domain", object_id=str(domain.pk), data={"contact_change_id": current.pk,
                "provider_reference": reference, "reason": reason.strip()})
    return current.status


def confirm_provider_completed(change_id, *, owner_id, reference, reason, provider_success_confirmed=False, client):
    """Apply live-matched associations after explicit owner/provider-case review.

    The owner attests the exact action's successful completion because legacy
    action-status enums are not published. Timestamps, live roles and applicable
    IRTP SUCCESS remain mandatory independent checks, never attestation overrides.
    """
    from types import SimpleNamespace
    from apps.domains.contact_change_confirmation import irtp_state
    from apps.domains.contact_changes import _locked_proposal, _live_association
    from apps.domains.publication_consent import REVIEWED_GTLDS
    from apps.domains.registrar_policy import ALL_ROLES, policy_for, tld_for_domain
    from apps.domains.registration_submission import _check_bindings

    if (provider_success_confirmed is not True or not isinstance(reference, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", reference)
            or not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 1000):
        raise ValidationError("Explicit provider-success attestation, a provider case and a review reason are required.")
    if not User.objects.filter(pk=owner_id, is_active=True, is_staff=True, is_superuser=True).exists():
        raise ValidationError("An active owner is required for completion review.")
    with transaction.atomic(durable=True):
        current, contacts = _locked_proposal(change_id, allow_withdrawn_publication=True)
        actor = User.objects.select_for_update().filter(pk=owner_id, is_active=True, is_staff=True, is_superuser=True).first()
        now = timezone.now()
        if (actor is None or current.status not in {"awaiting", "unknown"} or not identifier(current.action_id)
                or current.submitted_at is None or current.submitted_at > now - timedelta(minutes=15)
                or current.lease_until and current.lease_until > now or provider_scope(client) != current.provider_scope):
            raise ValidationError("Choose an awaiting request with no live worker and a submission at least 15 minutes old.")
        adapter = SimpleNamespace(tld=tld_for_domain(current.domain_name), **{
            role + "_id": getattr(current, role + "_id") for role in ALL_ROLES})
        _check_bindings(adapter, contacts, current.customer_id, current.provider_scope, current.role_ids)
        evidence = client.get_contact_change_action(current.registrar_order_id, current.action_id)
        if (not isinstance(evidence, ContactChangeAction) or evidence.order_id != current.registrar_order_id
                or evidence.action_id != current.action_id or not identifier(evidence.completed_at)
                or not int(current.submitted_at.timestamp()) <= int(evidence.completed_at) <= int(now.timestamp()) + 300):
            raise ValidationError("The exact provider action lacks a matching completion timestamp.")
        state = "not_applicable"
        if tld_for_domain(current.domain_name) in REVIEWED_GTLDS:
            state = irtp_state(client.get_irtp_status(current.registrar_order_id),
                               order_id=current.registrar_order_id, domain_name=current.domain_name)
            if state != "SUCCESS":
                raise ValidationError("Registrant authorization has not completed successfully.")
        _live_association(client.get_registration_reconciliation_details(current.registrar_order_id),
                          current, expected_roles=current.role_ids)
        domain = Domain.objects.get(pk=current.domain_id)  # Already locked by _locked_proposal.
        source, attempt, kind = locked_source(domain)
        fields = []
        for role in policy_for(tld_for_domain(current.domain_name)).roles:
            setattr(source, role + "_id", getattr(current, role + "_id"))
            fields.append(role)
        source.registrar_contact_ids = dict(current.role_ids)
        source.publication_decision_id = current.publication_decision_id
        source.save(update_fields=fields + ["registrar_contact_ids", "publication_decision", "updated_at"])
        current.status = "confirmed"
        current.confirmed_at = current.checked_at = now
        current.irtp_status = state
        current.lease_until = None
        current.token = uuid.uuid4()
        current.provider_observation = {"action_found": True, "action_status": evidence.status,
            "action_type": evidence.action_type, "action_completed_at": evidence.completed_at,
            "roles_match": True, "owner_attested_provider_success": True}
        current.save(update_fields=["status", "confirmed_at", "checked_at", "irtp_status", "lease_until", "token",
                                   "provider_observation", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.contact_change.confirmed", model_name="Domain",
            object_id=str(domain.pk), data={"contact_change_id": current.pk, "provider_reference": reference,
                "reason": reason.strip(), "owner_attested_provider_success": True})
    return "confirmed"
