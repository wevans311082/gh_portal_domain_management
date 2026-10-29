"""Durable same-contact consent revocation; never replaces a registrant."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.contact_bindings import provider_scope
from apps.domains.contact_change_confirmation import acknowledgement
from apps.domains.contact_changes import locked_source, _live_association
from apps.domains.models import Domain, DomainContactChange, PublicationRedaction, RegistrantPublicationDecision
from apps.domains.registrar_policy import workstream_tlds
from apps.domains.publication_verification import publication_observation
from apps.domains.registrar_policy import tld_for_domain


def _locked_context(job_id, client):
    from types import SimpleNamespace
    identity = PublicationRedaction.objects.values("decision__customer_id", "decision__domain_name", "decision_id").get(pk=job_id)
    User.objects.select_for_update().get(pk=identity["decision__customer_id"])
    domain = Domain.objects.select_for_update().filter(name=identity["decision__domain_name"], user_id=identity["decision__customer_id"]).first()
    if domain is None or tld_for_domain(domain.name) not in workstream_tlds("publication", "organisation_consent"):
        raise ValidationError("Publication redaction requires registrar association review.")
    source, attempt, kind = locked_source(domain)
    decision = RegistrantPublicationDecision.objects.select_for_update().get(pk=identity["decision_id"])
    job = PublicationRedaction.objects.select_for_update().get(pk=job_id)
    if (not decision.withdrawn_at or source.publication_decision_id != decision.pk
            or provider_scope(client) != attempt.provider_scope
            or DomainContactChange.objects.filter(domain=domain, status__in=["queued", "preparing", "submitting", "awaiting", "unknown"]).exists()):
        raise ValidationError("Publication redaction is awaiting a stable registrar association.")
    if job.provider_scope and (job.provider_scope != attempt.provider_scope or job.registrar_order_id != domain.registrar_id
            or job.customer_id != attempt.customer_id or job.role_ids != source.registrar_contact_ids):
        raise ValidationError("The publication redaction target changed after submission.")
    target = SimpleNamespace(domain_name=domain.name, registrar_order_id=domain.registrar_id,
        customer_id=attempt.customer_id, subject={"previous_remote_ids": source.registrar_contact_ids})
    return job, domain, attempt, source, target


def _read(job, target, client):
    payload = client.get_publication_reconciliation_details(target.registrar_order_id)
    _live_association(payload, target)
    evidence = publication_observation(payload, order_id=target.registrar_order_id,
        domain_name=target.domain_name, customer_id=target.customer_id)
    if not evidence.applicable:
        raise ValidationError("The registrar does not confirm applicable publication consent.")
    return evidence


def _record_revoked(job, domain):
    now = timezone.now()
    job.status = "confirmed"
    job.checked_at = job.confirmed_at = now
    job.save(update_fields=["status", "checked_at", "confirmed_at", "provider_scope", "registrar_order_id", "customer_id", "role_ids", "updated_at"])
    AuditLog.objects.create(action="domain.publication.provider_revoked", model_name="Domain",
        object_id=str(domain.pk), data={"publication_redaction_id": job.pk, "decision_id": job.decision_id})


def process_publication_redaction(job_id, *, client):
    """Read before mutation; a durable submission marker prohibits all replays."""
    with transaction.atomic(durable=True):
        job, domain, attempt, source, target = _locked_context(job_id, client)
        if job.status == "confirmed":
            return "confirmed"
        if job.status not in {"queued", "review", "awaiting", "unknown"}:
            return job.status
        if not job.provider_scope:
            job.provider_scope = attempt.provider_scope
            job.registrar_order_id = domain.registrar_id
            job.customer_id = attempt.customer_id
            job.role_ids = dict(source.registrar_contact_ids)
        evidence = _read(job, target, client)
        job.checked_at = timezone.now()
        if evidence.consent is False:
            _record_revoked(job, domain)
            return "confirmed"
        if job.status not in {"queued", "review"} or job.submitted_at is not None:
            if job.status in {"queued", "review"}:
                job.status = "unknown"
            job.available_at = timezone.now() + timedelta(minutes=5)
            job.save(update_fields=["status", "checked_at", "available_at", "updated_at"])
            return job.status
        # Null/never provided is not proof of explicit revocation: request false.
        job.provider_scope = attempt.provider_scope
        job.registrar_order_id = domain.registrar_id
        job.customer_id = attempt.customer_id
        job.role_ids = dict(source.registrar_contact_ids)
        job.status = "submitting"
        job.submitted_at = timezone.now()
        job.save(update_fields=["provider_scope", "registrar_order_id", "customer_id", "role_ids", "status", "submitted_at", "checked_at", "updated_at"])
        token = job.token
    try:
        with transaction.atomic(durable=True):
            job, domain, attempt, source, target = _locked_context(job_id, client)
            if job.status != "submitting" or job.token != token:
                raise ValidationError("The redaction submission requires review.")
            evidence = _read(job, target, client)
            if evidence.consent is False:
                _record_revoked(job, domain)
                return "confirmed"
            response = client.change_domain_contacts(job.registrar_order_id, job.role_ids,
                domain_name=domain.name, registrant_org_consent=False)
            accepted = acknowledgement(response, order_id=job.registrar_order_id)
            job.action_id = accepted.action_id
            job.status = "awaiting"
            job.available_at = timezone.now() + timedelta(minutes=5)
            job.save(update_fields=["action_id", "status", "available_at", "updated_at"])
        return "awaiting"
    except BaseException:
        PublicationRedaction.objects.filter(pk=job_id, token=token, status="submitting").update(status="unknown")
        raise
