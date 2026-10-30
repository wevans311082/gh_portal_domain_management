"""Durable managed-zone preparation. No records or delegation are changed."""
from dataclasses import asdict
from datetime import datetime, timedelta
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.cloudflare_integration.scope import assert_provider_scope
from apps.cloudflare_integration.services import CloudflareError, CloudflareService
from apps.cloudflare_integration.zones import _envelope, _zone, find_zone
from .contact_bindings import _fingerprint
from .dns_inventory import _locked, _validate
from .dns_changes import _manifest, zone_creation_options
from .dns_plan_review import _lock
from .dns_preflight import _approved, observe_execution_inputs
from .dns_target_inventory import _receipt
from .models import Domain, DomainDNSChange, DomainDNSExecutionStep
from .resellerclub_client import ResellerClubError
from apps.provisioning.whm_client import WHMClientError


def _intact(step, change):
    if (step.stage != "zone" or step.identity != change.target_provider or type(step.request) is not dict
            or change.manifest_digest != _manifest(change) or step.request_digest != _fingerprint(step.request)
            or step.request.get("target_digest") != change.target_approved_digest
            or step.request.get("manifest_digest") != change.manifest_digest
            or step.request.get("provider_scope") != change.provider_scope
            or step.request.get("effect") != "prepare_zone_only" or step.request.get("mode") not in ("create", "adopt")):
        raise ValidationError("The retained zone execution request requires review.")
    for item in [*step.preflights, *step.observations]:
        if type(item) is not dict:
            raise ValidationError("The retained zone execution evidence requires review.")
        body = dict(item)
        digest = body.pop("digest", None)
        if digest != _fingerprint(body):
            raise ValidationError("The retained zone execution evidence requires review.")
    if step.receipt_digest:
        if (step.receipt_digest != _fingerprint(step.receipt) or step.status != "complete"
                or step.completed_at is None or not step.provider_id or change.zone_id != step.provider_id):
            raise ValidationError("The retained zone execution receipt requires review.")


def _factual_lock(change_id):
    """Retain provider acknowledgements even if an actor was just revoked."""
    identity = DomainDNSChange.objects.values("user_id", "domain_id").get(pk=change_id)
    User.objects.select_for_update().get(pk=identity["user_id"])
    Domain.objects.select_for_update().get(pk=identity["domain_id"])
    change = DomainDNSChange.objects.select_for_update().get(pk=change_id)
    step = DomainDNSExecutionStep.objects.select_for_update().get(change=change, stage="zone", identity=change.target_provider)
    _intact(step, change)
    return change, step


def _lease(step, token):
    if step.token != token or step.lease_until is None or step.lease_until <= timezone.now():
        raise ValidationError("The zone execution lease changed or expired. Review its retained outcome.")


def _review(change_id, token):
    with transaction.atomic():
        change, step = _factual_lock(change_id)
        if step.token != token or step.receipt_digest:
            return step
        step.status, step.token, step.lease_until = "review", None, None
        step.message = "Zone preparation requires review. Reconcile any submitted provider request; do not repeat it."
        step.save(update_fields=["status", "token", "lease_until", "message", "updated_at"])
        if change.status != "cancelled":
            change.status = "review"
            change.save(update_fields=["status", "updated_at"])
        AuditLog.objects.create(user_id=change.requested_by_id, action="domain.dns.zone_review",
            model_name="DomainDNSExecutionStep", object_id=str(step.pk), data={"submitted": step.submitted_at is not None})
        return step


def prepare_cloudflare_zone(change_id, *, source_client, target_client, registrar_client):
    return _prepare_zone(change_id, provider="cloudflare", source_client=source_client, target_client=target_client, registrar_client=registrar_client)


def prepare_whm_zone(change_id, *, source_client, target_client, registrar_client):
    return _prepare_zone(change_id, provider="cpanel", source_client=source_client, target_client=target_client, registrar_client=registrar_client)


def _prepare_zone(change_id, *, provider, source_client, target_client, registrar_client):
    if connection.in_atomic_block:
        raise ValidationError("Run zone preparation outside a database transaction.")
    with transaction.atomic():
        actor, domain, change = _locked(change_id)
        if change.target_provider != provider:
            raise ValidationError("Choose the matching DNS provider execution adapter.")
        step = DomainDNSExecutionStep.objects.select_for_update().filter(change=change, stage="zone", identity=provider).first()
        if step:
            _intact(step, change)
            if step.receipt_digest or step.submitted_at is not None:
                return step  # Retained history; never re-POST after submission.
            if step.status != "preparing" or step.provider_id or step.completed_at is not None:
                raise ValidationError("This zone execution state requires reconciliation before another attempt.")
            if step.lease_until is not None and step.lease_until > timezone.now():
                return step
        _approved(change, domain, source_client, target_client, registrar_client)
        mode = "adopt" if change.target_receipt["target"]["exists"] else "create"
        body = {"version": 1, "mode": mode, "effect": "prepare_zone_only", "name": change.source["name"],
            "provider_scope": change.provider_scope,
            "manifest_digest": change.manifest_digest, "target_digest": change.target_approved_digest}
        if provider == "cloudflare":
            body["account_id"] = target_client.account_id
        else:
            body["zone_creation"] = change.zone_creation
            body["realm"] = {"endpoint": target_client.base_url, "username": target_client.username}
            if mode == "create" and (not change.zone_creation or zone_creation_options(provider, target_client) != change.zone_creation):
                raise ValidationError("Review explicit WHM zone creation settings before provisioning a missing zone.")
        if step is None:
            step = DomainDNSExecutionStep.objects.create(change=change, stage="zone", identity=provider,
                created_by=actor, request=body, request_digest=_fingerprint(body))
        elif step.request != body or len(step.preflights) >= 20:
            raise ValidationError("Review a new DNS request before preparing this zone.")
        token = uuid.uuid4()
        step.status, step.token, step.lease_until = "preparing", token, timezone.now() + timedelta(minutes=10)
        step.save(update_fields=["status", "token", "lease_until", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.dns.zone_started", model_name="DomainDNSExecutionStep",
            object_id=str(step.pk), data={"mode": mode})
    try:
        proof = observe_execution_inputs(change_id, source_client=source_client,
            target_client=target_client, registrar_client=registrar_client)
        with transaction.atomic():
            actor, domain, current = _locked(change_id)
            _approved(current, domain, source_client, target_client, registrar_client)
            step = DomainDNSExecutionStep.objects.select_for_update().get(pk=step.pk)
            _intact(step, current)
            _lease(step, token)
            if provider == "cpanel" and mode == "create" and zone_creation_options(provider, target_client) != current.zone_creation:
                raise ValidationError("WHM creation settings changed. Review a new request.")
            body = dict(proof)
            digest = body.pop("digest", None)
            if (digest != _fingerprint(body) or proof.get("source_digest") != current.inventory_digest
                    or proof.get("target_digest") != current.target_digest or proof.get("parent_ds_verified") is not True
                    or datetime.fromisoformat(proof["public_parent_ds"]["after"]["verified_until"]) <= timezone.now()):
                raise ValidationError("The zone preparation proof changed or expired.")
            step.preflights = [*step.preflights, proof]
            if mode == "create":
                step.submitted_at = current.zone_submitted_at = timezone.now()
                step.status, current.status = "submitting", "zone_submitting"
                current.save(update_fields=["zone_submitted_at", "status", "updated_at"])
            step.save(update_fields=["preflights", "submitted_at", "status", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.zone_authorized", model_name="DomainDNSExecutionStep",
                object_id=str(step.pk), data={"mode": mode, "scope": "zone_preparation_only", "delegation_authorized": False})
        if mode == "create":
            if provider == "cloudflare":
                assert_provider_scope(change.provider_scope, target_client)
                payload = target_client.create_managed_zone(change.source["name"], account_id=step.request["account_id"])
                assert_provider_scope(change.provider_scope, target_client)
                _envelope(payload)
                zone = _zone(payload.get("result"), CloudflareService._zone_name(change.source["name"]), target_client, change.provider_scope)
                if zone.paused:
                    raise ValidationError("The created zone is paused and requires review.")
                zone_id = zone.zone_id
            else:
                from .dns_changes import backend_scope
                if backend_scope(provider, target_client) != change.provider_scope:
                    raise ValidationError("The WHM zone-creation account changed.")
                if target_client.create_dns_zone(change.source["name"], ip=step.request["zone_creation"]["ip"]) is not True:
                    raise ValidationError("The WHM zone-creation response requires review.")
                if backend_scope(provider, target_client) != change.provider_scope:
                    raise ValidationError("The WHM zone-creation account changed.")
                zone_id = change.source["name"].lower().rstrip(".")
            with transaction.atomic():
                current, step = _factual_lock(change_id)
                if step.submitted_at is None or current.zone_submitted_at != step.submitted_at:
                    raise ValidationError("The submitted zone request requires review.")
                step.provider_id, current.zone_id = zone_id, zone_id
                step.save(update_fields=["provider_id", "updated_at"])
                current.save(update_fields=["zone_id", "updated_at"])
                AuditLog.objects.create(user_id=current.requested_by_id, action="domain.dns.zone_acknowledged",
                    model_name="DomainDNSExecutionStep", object_id=str(step.pk), data={"zone_id": zone_id})
        # Verify current authority before any further read following a POST.
        with transaction.atomic():
            actor, domain, current = _locked(change_id)
            _validate(current, domain, source_client, target_client, registrar_client)
            step = DomainDNSExecutionStep.objects.select_for_update().get(pk=step.pk)
            _lease(step, token)
        target = _receipt(current, target_client)
        if (not target["exists"] or (mode == "create" and target["zone"]["zone_id"] != step.provider_id)
                or (mode == "adopt" and _fingerprint(target) != _fingerprint(change.target_receipt["target"]))):
            raise ValidationError("The prepared zone could not be verified against this request.")
        needs_review = mode == "create" and bool(target["records"])
        receipt = {"version": 1, "mode": mode, "target": target, "record_review_required": needs_review,
            "delegation_updated": False, "provider_action_complete": True, "zone_preparation_complete": True}
        with transaction.atomic():
            actor, domain, current = _locked(change_id)
            _validate(current, domain, source_client, target_client, registrar_client)
            step = DomainDNSExecutionStep.objects.select_for_update().get(pk=step.pk)
            _intact(step, current)
            _lease(step, token)
            current.zone_id, current.zone_observation = target["zone"]["zone_id"], target["zone"]
            current.status = "zone_review" if needs_review else "zone_ready"
            current.save(update_fields=["zone_id", "zone_observation", "status", "updated_at"])
            step.provider_id, step.status, step.completed_at = current.zone_id, "complete", timezone.now()
            step.receipt, step.receipt_digest = receipt, _fingerprint(receipt)
            step.token = step.lease_until = None
            step.message = "Zone verified. Record and delegation stages remain pending."
            step.save(update_fields=["provider_id", "status", "completed_at", "receipt", "receipt_digest", "token",
                "lease_until", "message", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.zone_prepared", model_name="DomainDNSExecutionStep",
                object_id=str(step.pk), data={"zone_id": step.provider_id, "record_review_required": needs_review})
            return step
    except (ValidationError, PermissionDenied, CloudflareError, ResellerClubError, WHMClientError):
        return _review(change_id, token)


def _assert_scope(provider, expected, client):
    if provider == "cloudflare":
        assert_provider_scope(expected, client)
    else:
        from .dns_changes import backend_scope
        if backend_scope(provider, client) != expected:
            raise ValidationError("The DNS observation account changed.")


def inspect_cloudflare_zone_submission(*, actor, change_id, target_client):
    return _inspect_zone_submission(actor=actor, change_id=change_id, target_client=target_client, provider="cloudflare")


def inspect_whm_zone_submission(*, actor, change_id, target_client):
    return _inspect_zone_submission(actor=actor, change_id=change_id, target_client=target_client, provider="cpanel")


def _inspect_zone_submission(*, actor, change_id, target_client, provider):
    """Retain scoped candidates for explicit reconciliation; never re-POST/adopt."""
    if connection.in_atomic_block:
        raise ValidationError("Inspect submitted zone work outside a database transaction.")
    with transaction.atomic():
        actor, domain, change = _lock(actor, change_id)
        if change.target_provider != provider:
            raise ValidationError("Choose the matching DNS provider observation adapter.")
        step = DomainDNSExecutionStep.objects.select_for_update().get(change=change, stage="zone", identity=provider)
        _intact(step, change)
        if step.receipt_digest:
            return step
        if step.submitted_at is None or step.submitted_at != change.zone_submitted_at:
            raise ValidationError("There is no matching submitted zone request to reconcile.")
        if step.lease_until is not None and step.lease_until > timezone.now():
            return step
        if len(step.observations) >= 20:
            raise ValidationError("This zone request reached its observation limit and requires owner review.")
        _assert_scope(provider, change.provider_scope, target_client)
        token = uuid.uuid4()
        step.token, step.lease_until, step.status = token, timezone.now() + timedelta(minutes=3), "observing"
        step.save(update_fields=["token", "lease_until", "status", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.dns.zone_inspection_started", model_name="DomainDNSExecutionStep",
            object_id=str(step.pk), data={})
    try:
        target = _receipt(change, target_client) if provider == "cpanel" else None
        zone = (target["zone"] if target["exists"] else None) if target is not None else find_zone(
            domain_name=change.source["name"], client=target_client, expected_scope=change.provider_scope)
        observation = {"version": 1, "request_digest": step.request_digest, "observed_at": timezone.now().isoformat(),
            "zone": zone if provider == "cpanel" else asdict(zone) if zone else None,
            "association_verified": False, "retry_authorized": False}
        observation["digest"] = _fingerprint(observation)
        with transaction.atomic():
            actor, domain, change = _lock(actor, change_id)
            step = DomainDNSExecutionStep.objects.select_for_update().get(pk=step.pk)
            _intact(step, change)
            _lease(step, token)
            _assert_scope(provider, change.provider_scope, target_client)
            step.observations = [*step.observations, observation]
            step.status, step.token, step.lease_until = "review", None, None
            step.message = "Scoped zone evidence retained. Explicit reconciliation is required; no retry or adoption was authorized."
            step.save(update_fields=["observations", "status", "token", "lease_until", "message", "updated_at"])
            change.status = "review"
            change.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.zone_candidate_observed", model_name="DomainDNSExecutionStep",
                object_id=str(step.pk), data={"zone_found": zone is not None})
            return step
    except (ValidationError, PermissionDenied, CloudflareError, WHMClientError):
        return _review(change_id, token)
