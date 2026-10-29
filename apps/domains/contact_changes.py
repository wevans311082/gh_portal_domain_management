"""Durable customer proposals; current domain associations are never edited here."""

import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.contact_bindings import _fingerprint, provider_scope
from apps.domains.contact_change_confirmation import identifier
from apps.domains.models import (
    ContactVerification, ContactChangeQueueState, Domain, DomainContact, DomainContactChange,
    DomainOrder, DomainTransfer,
)
from apps.domains.publication_consent import capture_decision, publication_snapshot
from apps.domains.registrar_policy import ALL_ROLES, policy_for, tld_for_domain, contact_payload_tld

OPEN_STATUSES = ("queued", "preparing", "submitting", "awaiting", "unknown")
MAX_OPEN_CHANGES = 500
REMOTE_ROLES = dict(zip(ALL_ROLES, ("reg", "admin", "tech", "billing")))


def locked_source(domain):
    """Require a captured, accepted provider identity, never today's default account.

    Caller holds the domain lock. Paid-attempt contact snapshots stay immutable;
    the current association may legitimately differ after a confirmed change.
    """
    candidates = []
    for model, relation, kind in ((DomainOrder, "registration_attempt", "registration"),
                                  (DomainTransfer, "submission_attempt", "transfer")):
        source = model.objects.select_for_update().filter(domain_id=domain.pk).first()
        if source is not None:
            candidates.append((source, relation, kind))
    if len(candidates) != 1:
        raise ValidationError("The domain's current registrar association requires owner review.")
    source, relation, kind = candidates[0]
    attempt = getattr(source, relation, None)
    if attempt is not None:
        attempt = type(attempt).objects.select_for_update().get(pk=attempt.pk)
    scope = provider_scope()
    if (source.status != "completed" or source.user_id != domain.user_id
            or source.domain_name != domain.name or source.tld != tld_for_domain(domain.name)
            or not identifier(domain.registrar_id)
            or source.registrar_order_id != domain.registrar_id
            or attempt is None or attempt.status != "accepted" or attempt.legacy_review_required
            or attempt.remote_order_id != domain.registrar_id or attempt.provider_scope != scope
            or not identifier(attempt.customer_id)
            or not isinstance(source.registrar_contact_ids, dict)
            or source.registrar_contact_ids.get("_scope") != scope):
        raise ValidationError("The domain's current registrar association requires owner review.")
    policy = policy_for(tld_for_domain(domain.name))
    if any(not identifier(source.registrar_contact_ids.get(REMOTE_ROLES[role])) for role in policy.roles):
        raise ValidationError("The current registrar contact association requires owner review.")
    return source, attempt, kind


def request_contact_change(*, customer, domain_id, key, contacts, publication_choice="redact"):
    """Queue one validated proposal atomically, without provider HTTP or mail.

    A reused key returns its original request only for the identical proposal.
    Different requests cannot share an open domain slot, including uncertain work.
    """
    try:
        key = uuid.UUID(str(key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Reload the contact-change form before submitting.") from None
    if not isinstance(contacts, dict) or set(contacts) - set(ALL_ROLES):
        raise ValidationError("Submit the domain's applicable contact roles.")
    with transaction.atomic(durable=True):
        ContactChangeQueueState.objects.get_or_create(pk=1)
        ContactChangeQueueState.objects.select_for_update().get(pk=1)
        owner = User.objects.select_for_update().get(pk=customer.pk)
        if not owner.is_active or owner.is_staff or owner.is_superuser:
            raise PermissionDenied
        domain = Domain.objects.select_for_update().filter(pk=domain_id, user=owner).first()
        if domain is None:
            raise PermissionDenied
        if domain.status != Domain.STATUS_ACTIVE:
            raise ValidationError("Contact changes require an active domain.")
        policy = policy_for(tld_for_domain(domain.name))
        selected = {}
        for role in ALL_ROLES:
            value = contacts.get(role)
            if role not in policy.roles:
                if value is not None:
                    raise ValidationError("This extension does not accept that contact role.")
                continue
            if type(value) is not int:
                raise ValidationError("Choose an owned contact for every applicable role.")
        # Stable locking order even when one contact fills several roles.
        rows = {row.pk: row for row in DomainContact.objects.select_for_update().filter(
            pk__in=[contacts[role] for role in policy.roles], user=owner).order_by("pk")}
        for role in policy.roles:
            selected[role] = rows.get(contacts[role])
            if selected[role] is None:
                raise PermissionDenied
        source, attempt, kind = locked_source(domain)
        digests = {}
        for role, contact in selected.items():
            contact.as_resellerclub_payload(attempt.customer_id, contact_payload_tld(tld_for_domain(domain.name), role))
            digests[role] = ContactVerification.digest_for(contact)
        proposal = {"roles": {role: contact.pk for role, contact in selected.items()},
                    "contact_digests": digests, "publication_choice": publication_choice}
        proposal_digest = _fingerprint(proposal)
        existing = DomainContactChange.objects.select_for_update().filter(key=key).first()
        if existing:
            if (existing.user_id != owner.pk or existing.domain_id != domain.pk
                    or existing.subject.get("proposal_digest") != proposal_digest):
                raise ValidationError("This request key has already been used. Reload the form.")
            return existing
        if DomainContactChange.objects.filter(domain=domain, status__in=OPEN_STATUSES).exists():
            raise ValidationError("A contact change is already open for this domain. Review its progress first.")
        if DomainContactChange.objects.filter(status__in=OPEN_STATUSES).count() >= MAX_OPEN_CHANGES:
            raise ValidationError("The registrar contact-change queue is busy. Try again later.")
        decision = capture_decision(customer=owner, contact=selected["registration_contact"],
                                    domain_name=domain.name, choice=publication_choice)
        change = DomainContactChange(domain=domain, user=owner, key=key, domain_name=domain.name,
            registrar_order_id=domain.registrar_id, provider_scope=attempt.provider_scope,
            customer_id=attempt.customer_id, publication_decision=decision, **selected)
        change.subject = {**proposal, "proposal_digest": proposal_digest,
            "source_kind": kind, "source_id": source.pk,
            "previous_roles": {role: getattr(source, role + "_id") for role in policy.roles},
            "previous_remote_ids": dict(source.registrar_contact_ids),
            "publication": publication_snapshot(change, selected["registration_contact"])}
        change.save()
        AuditLog.objects.create(user=owner, action="domain.contact_change.queued", model_name="Domain",
            object_id=str(domain.pk), data={"contact_change_id": change.pk})
        from apps.domains.contact_change_jobs import publish_contact_change
        transaction.on_commit(lambda: publish_contact_change(change.pk))
        return change


def _locked_proposal(change_id, *, allow_withdrawn_publication=False):
    """Re-read all mutable subjects using the same lock order as admission."""
    identity = DomainContactChange.objects.values("user_id", "domain_id").get(pk=change_id)
    owner = User.objects.select_for_update().get(pk=identity["user_id"])
    domain = Domain.objects.select_for_update().get(pk=identity["domain_id"])
    proposal = DomainContactChange.objects.get(pk=change_id)
    roles = policy_for(tld_for_domain(domain.name)).roles
    contacts = {row.pk: row for row in DomainContact.objects.select_for_update().filter(
        pk__in=[getattr(proposal, role + "_id") for role in roles]).order_by("pk")}
    source, attempt, kind = locked_source(domain)
    change = DomainContactChange.objects.select_for_update().get(pk=change_id)
    if (not owner.is_active or owner.is_staff or owner.is_superuser or domain.user_id != owner.pk
            or domain.status != Domain.STATUS_ACTIVE or domain.name != change.domain_name
            or domain.registrar_id != change.registrar_order_id or attempt.customer_id != change.customer_id
            or attempt.provider_scope != change.provider_scope or source.pk != change.subject.get("source_id")
            or kind != change.subject.get("source_kind")
            or source.registrar_contact_ids != change.subject.get("previous_remote_ids")
            or {role: getattr(source, role + "_id") for role in roles} != change.subject.get("previous_roles")
            or {role: getattr(change, role + "_id") for role in roles} != change.subject.get("roles")):
        raise ValidationError("The contact-change association changed. Review the request.")
    for role in roles:
        contact = contacts.get(getattr(change, role + "_id"))
        if (contact is None or contact.user_id != owner.pk
                or ContactVerification.digest_for(contact) != change.subject.get("contact_digests", {}).get(role)):
            raise ValidationError("A proposed contact changed. Review the request.")
        contact.as_resellerclub_payload(change.customer_id, contact_payload_tld(tld_for_domain(domain.name), role))
    publication = publication_snapshot(change, contacts[change.registration_contact_id])
    if publication != change.subject.get("publication"):
        from apps.domains.models import RegistrantPublicationDecision
        retained = change.subject.get("publication", {})
        withdrawn_after_submission = (allow_withdrawn_publication and change.submitted_at is not None
            and change.publication_decision_id is not None and retained.get("decision_id") == change.publication_decision_id
            and retained.get("consent") is True
            and publication == {"decision_id": change.publication_decision_id, "consent": False}
            and RegistrantPublicationDecision.objects.filter(pk=change.publication_decision_id, withdrawn_at__isnull=False).exists())
        if not withdrawn_after_submission:
            raise ValidationError("The publication choice changed. Review the request.")
    return change, contacts


def _live_association(payload, change, *, expected_roles=None):
    """Match live provider order/customer/roles before creating any contacts."""
    if (not isinstance(payload, dict) or identifier(payload.get("orderid", payload.get("entityid"))) != change.registrar_order_id
            or payload.get("domainname") != change.domain_name
            or identifier(payload.get("customerid")) != change.customer_id
            or payload.get("currentstatus") != "Active"):
        raise ValidationError("The live registrar association requires review.")
    fields = {"reg": "registrantcontactid", "admin": "admincontactid", "tech": "techcontactid", "billing": "billingcontactid"}
    for role in policy_for(tld_for_domain(change.domain_name)).roles:
        key = REMOTE_ROLES[role]
        if identifier(payload.get(fields[key])) != (expected_roles if expected_roles is not None else change.subject["previous_remote_ids"])[key]:
            raise ValidationError("The live registrar contacts changed. Review the request.")


def _uk_identity_unchanged(change, contacts, client):
    """Compare the actual bound provider contact, never an edited local old row.

    Contact-person and holder names remain exact. Corrections, entity changes and
    ambiguous legacy identities need registry review rather than a free update.
    No provider contact details are copied into the request or audit trail.
    """
    if policy_for(tld_for_domain(change.domain_name)).contact_type != "UkContact":
        return
    old_id = change.subject["previous_remote_ids"]["reg"]
    payload = client.get_contact(old_id)
    proposed = contacts[change.registration_contact_id]
    valid = (isinstance(payload, dict) and identifier(payload.get("entityid")) == old_id
        and identifier(payload.get("customerid")) == change.customer_id and payload.get("type") == "UkContact"
        and str(payload.get("status", "")).upper() != "ERROR"
        and isinstance(payload.get("name"), str) and isinstance(payload.get("company"), str))
    if not valid:
        raise ValidationError("The UK registry contact identity requires owner review.")
    same_name = payload["name"].strip() == proposed.name.strip()
    company = payload["company"].strip()
    if proposed.registrant_type == "individual":
        same_holder = company in {"", "N/A"} and not proposed.company.strip() and not proposed.company_number.strip()
    else:
        same_holder = bool(company) and company not in {"N/A", "NA"} and company == proposed.company.strip()
    if not same_name or not same_holder:
        raise ValidationError("Changing the UK registrant identity requires the Nominet registrant-transfer workflow. Contact support.")


def submit_contact_change(change_id, *, client):
    """One durable submission; awaiting/uncertain calls are never replayed.

    Contact creation has its own durable reservations. A preparation crash stays
    preparing for explicit recovery; it is never automatically reclaimed.
    """
    from apps.domains.contact_bindings import resolve_contact_binding
    from apps.domains.contact_change_confirmation import acknowledgement
    from apps.domains.registration_submission import _check_bindings
    from types import SimpleNamespace

    with transaction.atomic(durable=True):
        change, contacts = _locked_proposal(change_id)
        if change.status != "queued":
            return change.status
        if provider_scope(client) != change.provider_scope:
            raise ValidationError("The registrar environment changed. Review the request.")
        change.status = "preparing"
        change.save(update_fields=["status", "updated_at"])
    token = change.token
    try:
        _live_association(client.get_registration_reconciliation_details(change.registrar_order_id), change)
        _uk_identity_unchanged(change, contacts, client)
        role_ids = {"_scope": change.provider_scope, "admin": "-1", "tech": "-1", "billing": "-1"}
        for role in policy_for(tld_for_domain(change.domain_name)).roles:
            contact = contacts[getattr(change, role + "_id")]
            role_ids[REMOTE_ROLES[role]] = resolve_contact_binding(contact, change.customer_id,
                contact_payload_tld(tld_for_domain(change.domain_name), role), client=client)
        with transaction.atomic(durable=True):
            current, contacts = _locked_proposal(change_id)
            if current.status != "preparing" or current.token != token:
                raise ValidationError("The contact-change worker was superseded.")
            adapter = SimpleNamespace(tld=tld_for_domain(current.domain_name), **{
                role + "_id": getattr(current, role + "_id") for role in ALL_ROLES})
            _check_bindings(adapter, contacts, current.customer_id, current.provider_scope, role_ids)
            current.status = "submitting"
            current.role_ids = role_ids
            current.submitted_at = timezone.now()
            current.save(update_fields=["status", "role_ids", "submitted_at", "updated_at"])
        with transaction.atomic(durable=True):
            current, contacts = _locked_proposal(change_id)
            if current.status != "submitting" or current.token != token or provider_scope(client) != current.provider_scope:
                raise ValidationError("The contact-change submission requires review.")
            _check_bindings(adapter, contacts, current.customer_id, current.provider_scope, role_ids)
            _live_association(client.get_registration_reconciliation_details(current.registrar_order_id), current)
            _uk_identity_unchanged(current, contacts, client)
            result = client.change_domain_contacts(current.registrar_order_id, role_ids,
                domain_name=current.domain_name, registrant_org_consent=current.subject["publication"]["consent"])
            evidence = acknowledgement(result, order_id=current.registrar_order_id)
            current.status = "awaiting"
            current.action_id = evidence.action_id
            current.save(update_fields=["status", "action_id", "updated_at"])
            AuditLog.objects.create(action="domain.contact_change.submitted", model_name="Domain",
                object_id=str(current.domain_id), data={"contact_change_id": current.pk})
        return "awaiting"
    except BaseException:
        # Never persist raw provider exceptions or reopen a durable call marker.
        DomainContactChange.objects.filter(pk=change_id, token=token, status="preparing").update(status="failed")
        DomainContactChange.objects.filter(pk=change_id, token=token, status="submitting").update(status="unknown")
        raise


def observe_contact_change(change_id, *, client):
    """Read and retain correlated provider facts without applying associations.

    Pending authorization, unknown action statuses and provider facts are distinct
    from local success. Changed local contact data does not suppress these reads.
    This function never creates contacts or repeats a modify-contact request.
    """
    from datetime import timedelta
    from apps.domains.contact_change_confirmation import ContactChangeAction, irtp_state
    from apps.domains.registrar_policy import workstream_tlds

    identity = DomainContactChange.objects.values("user_id", "domain_id").get(pk=change_id)
    with transaction.atomic(durable=True):
        User.objects.select_for_update().get(pk=identity["user_id"])
        domain = Domain.objects.select_for_update().get(pk=identity["domain_id"])
        source, attempt, kind = locked_source(domain)
        change = DomainContactChange.objects.select_for_update().get(pk=change_id)
        if change.status not in {"awaiting", "unknown"}:
            return change.status
        if (domain.user_id != change.user_id or domain.name != change.domain_name
                or domain.registrar_id != change.registrar_order_id or change.customer_id != attempt.customer_id
                or change.provider_scope != attempt.provider_scope or provider_scope(client) != change.provider_scope
                or source.pk != change.subject.get("source_id") or kind != change.subject.get("source_kind")
                or not identifier(change.action_id)):
            raise ValidationError("The contact-change observation requires owner review.")
        action = client.get_contact_change_action(change.registrar_order_id, change.action_id)
        if action is not None and (not isinstance(action, ContactChangeAction)
                or action.order_id != change.registrar_order_id or action.action_id != change.action_id):
            raise ValidationError("The registrar action could not be matched.")
        state = "not_applicable"
        if tld_for_domain(change.domain_name) in workstream_tlds("contact_change", "icann_irtp"):
            state = irtp_state(client.get_irtp_status(change.registrar_order_id),
                               order_id=change.registrar_order_id, domain_name=change.domain_name)
        payload = client.get_registration_reconciliation_details(change.registrar_order_id)
        # Identity errors must not be reduced to a harmless contact mismatch.
        expected = dict(change.role_ids)
        if any(not identifier(expected.get(REMOTE_ROLES[role]))
               for role in policy_for(tld_for_domain(change.domain_name)).roles):
            raise ValidationError("The submitted contact identifiers require review.")
        matching = True
        try:
            _live_association(payload, change, expected_roles=expected)
        except ValidationError:
            matching = False
            # Still prove identity using the actual returned roles; otherwise
            # a foreign account/domain could be recorded as a pending update.
            fields = {"reg": "registrantcontactid", "admin": "admincontactid", "tech": "techcontactid", "billing": "billingcontactid"}
            actual = {key: identifier(payload.get(field)) for key, field in fields.items()} if isinstance(payload, dict) else {}
            if any(not actual.get(REMOTE_ROLES[role]) for role in policy_for(tld_for_domain(change.domain_name)).roles):
                raise ValidationError("The registrar contact identifiers require review.") from None
            _live_association(payload, change, expected_roles=actual)
        now = timezone.now()
        change.irtp_status = state
        change.provider_observation = {"action_found": action is not None,
            "action_status": action.status if action else "", "action_type": action.action_type if action else "",
            "action_completed_at": action.completed_at if action else "", "roles_match": matching}
        change.checked_at = now
        change.available_at = now + timedelta(minutes=5)
        change.save(update_fields=["irtp_status", "provider_observation", "checked_at", "available_at", "updated_at"])
        return change.status
