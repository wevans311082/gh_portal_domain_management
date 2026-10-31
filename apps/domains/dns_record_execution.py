"""Journaled target record preparation. No registrar or active-provider changes."""
from datetime import datetime, timedelta
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.cloudflare_integration.services import CloudflareError
from apps.provisioning.whm_client import WHMClientError
from .contact_bindings import _fingerprint
from .dns_apply_review import approved_apply_evidence
from .dns_plan_review import _lock
from .dns_inventory import _locked, _validate, read_source_receipt
from .dns_parent_ds import read_parent_ds
from .dns_payloads import compile_record_payload
from .dns_record_plan import build_record_plan
from .dns_security import provider_dnssec, registrar_dnssec
from .dns_target_inventory import _receipt
from .dns_zone_execution import _assert_scope, _lease
from .models import Domain, DomainDNSChange, DomainDNSExecutionStep
from .resellerclub_client import ResellerClubError


ERRORS = (ValidationError, PermissionDenied, CloudflareError, WHMClientError, ResellerClubError)


def _approved(change, domain, source_client, target_client, registrar_client):
    plan = approved_apply_evidence(change, domain)
    if (change.status not in {"records_approved", "record_writing"} or change.cancelled_at is not None
            or change.completed_at is not None or plan.expires_at <= timezone.now()):
        raise ValidationError("Current record consent is required before preparing target records.")
    _validate(change, domain, source_client, target_client, registrar_client)
    return plan


def _intact(step, change):
    body = step.request
    if (step.stage != "records" or step.identity != change.target_provider or type(body) is not dict
            or step.request_digest != _fingerprint(body) or body.get("manifest_digest") != change.manifest_digest
            or body.get("provider_scope") != change.provider_scope or body.get("zone_id") != change.zone_id
            or body.get("effect") != "prepare_records_only" or body.get("plan_id") != change.approved_apply_plan_id
            or body.get("payload_digest") != _fingerprint(body.get("payload"))):
        raise ValidationError("The retained record execution intent requires review.")
    for value in [*step.preflights, *step.observations]:
        if type(value) is not dict:
            raise ValidationError("The retained record execution evidence requires review.")
        data = dict(value)
        if data.pop("digest", None) != _fingerprint(data):
            raise ValidationError("The retained record execution evidence requires review.")
    if step.receipt_digest and (step.receipt_digest != _fingerprint(step.receipt)
            or step.completed_at is None or step.status != "complete"):
        raise ValidationError("The retained record execution receipt requires review.")


def _facts(change_id):
    identity = DomainDNSChange.objects.values("user_id", "domain_id").get(pk=change_id)
    User.objects.select_for_update().get(pk=identity["user_id"])
    Domain.objects.select_for_update().get(pk=identity["domain_id"])
    change = DomainDNSChange.objects.select_for_update().get(pk=change_id)
    step = DomainDNSExecutionStep.objects.select_for_update().get(change=change, stage="records", identity=change.target_provider)
    _intact(step, change)
    return change, step


def _review(change_id, token):
    with transaction.atomic():
        change, step = _facts(change_id)
        if step.receipt_digest or step.token != token:
            return step
        step.status, step.token, step.lease_until = "review", None, None
        step.message = "Record preparation requires reconciliation. Do not repeat a submitted batch."
        step.save(update_fields=["status", "token", "lease_until", "message", "updated_at"])
        if change.cancelled_at is None:
            change.status = "record_review"
            change.save(update_fields=["status", "updated_at"])
        AuditLog.objects.create(user_id=change.requested_by_id, action="domain.dns.records_review",
            model_name="DomainDNSExecutionStep", object_id=str(step.pk), data={"submitted": step.submitted_at is not None})
        return step


def _observe(change, plan, *, source_client, target_client, registrar_client, mutating):
    target = plan.plan["records"]["target"]
    def security():
        return {"registrar": registrar_dnssec(source=change.source, client=registrar_client),
            "source": provider_dnssec(provider=change.source["dns_provider"], name=change.source["name"],
                zone_id=change.inventory_receipt["zone"]["zone_id"], client=source_client, scope=change.source["dns_source_scope"]),
            "target": provider_dnssec(provider=change.target_provider, name=change.source["name"],
                zone_id=change.zone_id, client=target_client, scope=change.provider_scope)}
    before, parent_before = security(), read_parent_ds(change.source["name"])
    source = read_source_receipt(change, source_client, registrar_client)
    fresh_target = _receipt(change, target_client)
    after, parent_after = security(), read_parent_ds(change.source["name"])
    if (_fingerprint(source) != change.inventory_digest or _fingerprint(fresh_target) != _fingerprint(target)
            or before != after or parent_before["parent_zone"] != parent_after["parent_zone"]
            or after["registrar"]["nameservers"] != sorted(source["registrar_nameservers"])):
        raise ValidationError("DNS records, security or delegation changed after record consent.")
    if mutating and set(source["registrar_nameservers"]) & set(target["zone"]["nameservers"]):
        raise ValidationError("The target shares live authoritative servers; review a separate live-zone change.")
    if datetime.fromisoformat(parent_after["verified_until"]) <= timezone.now():
        raise ValidationError("The parent DS observation expired before record preparation.")
    body = {"version": 1, "observed_at": timezone.now().isoformat(), "plan_digest": plan.digest,
        "source_digest": change.inventory_digest, "target_digest": _fingerprint(fresh_target), "dnssec": after,
        "public_parent_ds": {"before": parent_before, "after": parent_after}, "delegation_authorized": False}
    return {**body, "digest": _fingerprint(body)}


def _matches(records, target):
    if not target.get("exists") or target.get("zone", {}).get("zone_id") != records["target"]["zone"]["zone_id"]:
        raise ValidationError("The target zone association changed during record preparation.")
    inventory = {"zone": target["zone"], "records": target["records"]}
    observed = build_record_plan(source_provider=records["provider"], target_provider=records["provider"],
        zone_name=records["zone_name"], inventory_receipt=inventory, inventory_digest=_fingerprint(inventory))
    if observed["blocked"]:
        raise ValidationError("The resulting target inventory requires review.")
    actual = {_fingerprint(entry["record"]) for entry in observed["entries"] if entry["disposition"] in {"copy", "duplicate"}}
    if actual != {_fingerprint(record) for record in records["final_records"]}:
        raise ValidationError("The complete target answers do not match the approved final records.")
    indexed = {entry["source_ref"]: entry for entry in observed["entries"]}
    for action in records["actions"]:
        if action["action"] in {"keep", "retain"}:
            for before in action["before"]:
                after = indexed.get(before["source_ref"])
                if after is None or _fingerprint(after["record"]) != _fingerprint(before["record"]):
                    raise ValidationError("An unchanged target record was removed or modified.")
    # Authority is never taken from the source plan. Only the WHM SOA serial
    # may change as a result of a serial-guarded edit.
    from .dns_payloads import _rdata
    def managed(entries):
        values = []
        for entry in entries:
            if entry["disposition"] == "provider_managed":
                record = dict(entry["record"])
                if record["type"] == "SOA":
                    value = _rdata(record).replace(serial=0)
                    record["rdata_wire"], record["rdata_text"] = value.to_wire(canonicalize=True).hex(), value.to_text()
                values.append(_fingerprint(record))
        return sorted(values)
    if managed(observed["entries"]) != managed(records["target_plan"]["entries"]):
        raise ValidationError("Provider-managed authority changed during record preparation.")
    metadata = {entry["source"] for entry in observed["entries"] if entry["disposition"] == "metadata"}
    if any(entry["source"] not in metadata for entry in records["target_plan"]["entries"] if entry["disposition"] == "metadata"):
        raise ValidationError("Target zone metadata was not preserved.")


def prepare_target_records(change_id, *, source_client, target_client, registrar_client):
    if connection.in_atomic_block:
        raise ValidationError("Run record preparation outside a database transaction.")
    with transaction.atomic():
        actor, domain, change = _locked(change_id)
        step = DomainDNSExecutionStep.objects.select_for_update().filter(change=change, stage="records", identity=change.target_provider).first()
        if step:
            _intact(step, change)
            if step.receipt_digest or step.submitted_at is not None:
                return step
            if step.status != "preparing" or step.completed_at is not None or step.provider_id:
                raise ValidationError("Reconcile this record request before another attempt.")
            if step.lease_until is not None and step.lease_until > timezone.now():
                return step
        plan = _approved(change, domain, source_client, target_client, registrar_client)
        payload = compile_record_payload(plan.plan["records"])
        body = {"version": 1, "effect": "prepare_records_only", "manifest_digest": change.manifest_digest,
            "provider_scope": change.provider_scope, "zone_id": change.zone_id, "plan_id": plan.pk,
            "plan_digest": plan.digest, "payload": payload, "payload_digest": _fingerprint(payload)}
        if step is None:
            step = DomainDNSExecutionStep.objects.create(change=change, stage="records", identity=change.target_provider,
                created_by=actor, request=body, request_digest=_fingerprint(body))
        elif _fingerprint(body) != step.request_digest or len(step.preflights) >= 20:
            raise ValidationError("The record execution intent requires a new review.")
        token = uuid.uuid4()
        step.token, step.lease_until = token, timezone.now() + timedelta(minutes=10)
        step.save(update_fields=["token", "lease_until", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.dns.records_started", model_name="DomainDNSExecutionStep", object_id=str(step.pk), data={})
    mutating = bool(payload.get("deletes") or payload.get("posts") or payload.get("remove") or payload.get("add"))
    try:
        proof = _observe(change, plan, source_client=source_client, target_client=target_client,
            registrar_client=registrar_client, mutating=mutating)
        with transaction.atomic():
            actor, domain, current = _locked(change_id)
            approved = _approved(current, domain, source_client, target_client, registrar_client)
            step = DomainDNSExecutionStep.objects.select_for_update().get(pk=step.pk)
            _intact(step, current)
            _lease(step, token)
            if approved.digest != step.request["plan_digest"] or datetime.fromisoformat(proof["public_parent_ds"]["after"]["verified_until"]) <= timezone.now():
                raise ValidationError("Record approval or security evidence changed before submission.")
            step.preflights = [*step.preflights, proof]
            step.status = "submitting" if mutating else "observing"
            if mutating:
                step.submitted_at = timezone.now()
                current.status = "record_writing"
                current.save(update_fields=["status", "updated_at"])
            step.save(update_fields=["preflights", "status", "submitted_at", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.records_authorized", model_name="DomainDNSExecutionStep",
                object_id=str(step.pk), data={"payload_digest": step.request["payload_digest"], "mutating": mutating, "delegation_authorized": False})
        if mutating:
            _assert_scope(change.target_provider, change.provider_scope, target_client)
            acknowledgement = target_client.apply_dns_batch(change.zone_id, payload) if change.target_provider == "cloudflare" else target_client.apply_dns_batch(payload)
            if type(acknowledgement) is not dict or (change.target_provider == "cloudflare" and (
                    acknowledgement.get("success") is not True or type(acknowledgement.get("result")) is not dict)) or (
                    change.target_provider == "cpanel" and (type(acknowledgement.get("new_serial")) is not int
                    or not 0 <= acknowledgement["new_serial"] <= 4294967295
                    or acknowledgement["new_serial"] == payload["serial"])):
                raise ValidationError("The record batch acknowledgement requires reconciliation.")
            # Keep factual acknowledgements even after revocation or lease loss.
            ack = {"kind": "provider_ack", "observed_at": timezone.now().isoformat(),
                "new_serial": acknowledgement.get("new_serial") if change.target_provider == "cpanel" else None}
            ack["digest"] = _fingerprint(ack)
            with transaction.atomic():
                current, step = _facts(change_id)
                step.provider_id = current.zone_id
                step.observations = [*step.observations, ack]
                step.save(update_fields=["provider_id", "observations", "updated_at"])
                AuditLog.objects.create(user_id=current.requested_by_id, action="domain.dns.records_acknowledged",
                    model_name="DomainDNSExecutionStep", object_id=str(step.pk), data={"payload_digest": step.request["payload_digest"]})
        with transaction.atomic():
            actor, domain, current = _locked(change_id)
            _approved(current, domain, source_client, target_client, registrar_client)
            step = DomainDNSExecutionStep.objects.select_for_update().get(pk=step.pk)
            _intact(step, current)
            _lease(step, token)
        target = _receipt(current, target_client)
        _matches(plan.plan["records"], target)
        if mutating and change.target_provider == "cpanel" and target["zone"]["serial"] != acknowledgement["new_serial"]:
            raise ValidationError("The WHM serial changed after its batch acknowledgement.")
        source = read_source_receipt(current, source_client, registrar_client)
        if _fingerprint(source) != current.inventory_digest:
            raise ValidationError("The source delegation or records changed during target preparation.")
        receipt = {"version": 1, "target": target, "plan_digest": plan.digest, "records_preparation_complete": True,
            "provider_action_complete": True, "delegation_updated": False, "public_activation_verified": False}
        with transaction.atomic():
            actor, domain, current = _locked(change_id)
            _approved(current, domain, source_client, target_client, registrar_client)
            step = DomainDNSExecutionStep.objects.select_for_update().get(pk=step.pk)
            _intact(step, current)
            _lease(step, token)
            step.status, step.completed_at = "complete", timezone.now()
            step.receipt, step.receipt_digest = receipt, _fingerprint(receipt)
            step.token = step.lease_until = None
            step.message = "Target records verified. Registrar delegation and public activation remain pending."
            step.save(update_fields=["status", "completed_at", "receipt", "receipt_digest", "token", "lease_until", "message", "updated_at"])
            current.status = "records_ready"
            current.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.records_prepared", model_name="DomainDNSExecutionStep",
                object_id=str(step.pk), data={"plan_digest": plan.digest, "delegation_updated": False})
            return step
    except ERRORS:
        return _review(change_id, token)


def inspect_target_records(*, actor, change_id, source_client, target_client, registrar_client):
    """Retain complete scoped readback of a submitted batch; never repeat it."""
    if connection.in_atomic_block:
        raise ValidationError("Inspect submitted records outside a database transaction.")
    with transaction.atomic():
        actor, domain, change = _lock(actor, change_id)
        approved_apply_evidence(change, domain)
        step = DomainDNSExecutionStep.objects.select_for_update().get(change=change, stage="records", identity=change.target_provider)
        _intact(step, change)
        if step.receipt_digest:
            return step
        if step.submitted_at is None or change.cancelled_at is not None:
            raise ValidationError("There is no retained submitted record batch to reconcile.")
        if step.lease_until is not None and step.lease_until > timezone.now():
            return step
        if len(step.observations) >= 20:
            raise ValidationError("The record observation limit requires separate owner recovery evidence.")
        _validate(change, domain, source_client, target_client, registrar_client)
        token = uuid.uuid4()
        step.status, step.token, step.lease_until = "observing", token, timezone.now() + timedelta(minutes=3)
        step.save(update_fields=["status", "token", "lease_until", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.dns.records_inspection_started",
            model_name="DomainDNSExecutionStep", object_id=str(step.pk), data={})
    try:
        target = _receipt(change, target_client)
        body = {"kind": "records_inspection", "request_digest": step.request_digest,
            "target": target, "target_digest": _fingerprint(target), "observed_at": timezone.now().isoformat(),
            "retry_authorized": False, "delegation_authorized": False}
        body["digest"] = _fingerprint(body)
        with transaction.atomic():
            actor, domain, current = _lock(actor, change_id)
            _validate(current, domain, source_client, target_client, registrar_client)
            step = DomainDNSExecutionStep.objects.select_for_update().get(pk=step.pk)
            _intact(step, current)
            _lease(step, token)
            step.observations = [*step.observations, body]
            step.status, step.token, step.lease_until = "review", None, None
            step.message = "Scoped record evidence retained. Explicit reconciliation is required; no retry was authorized."
            step.save(update_fields=["observations", "status", "token", "lease_until", "message", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.records_inspected", model_name="DomainDNSExecutionStep",
                object_id=str(step.pk), data={"target_digest": body["target_digest"]})
            return step
    except ERRORS:
        return _review(change_id, token)


def reconcile_target_records(*, actor, change_id, observation_digest, confirmed,
                             source_client, target_client, registrar_client):
    """Accept an exact observed final zone without asserting batch provenance."""
    if confirmed is not True or type(observation_digest) is not str:
        raise ValidationError("Explicitly confirm the exact inspected record outcome.")
    if connection.in_atomic_block:
        raise ValidationError("Reconcile submitted records outside a database transaction.")
    with transaction.atomic():
        actor, domain, change = _lock(actor, change_id)
        plan = approved_apply_evidence(change, domain)
        step = DomainDNSExecutionStep.objects.select_for_update().get(change=change, stage="records", identity=change.target_provider)
        _intact(step, change)
        selected = next((value for value in step.observations if value.get("kind") == "records_inspection"
            and value.get("digest") == observation_digest), None)
        if selected is None or selected.get("request_digest") != step.request_digest:
            raise ValidationError("Choose retained inspection evidence belonging to this exact batch.")
        if step.receipt_digest:
            if step.receipt.get("reconciled_observation_digest") != observation_digest:
                raise ValidationError("Retain the original record reconciliation decision.")
            return step
        if (step.submitted_at is None or change.cancelled_at is not None
                or datetime.fromisoformat(selected["observed_at"]) + timedelta(minutes=30) <= timezone.now()):
            raise ValidationError("Inspect current submitted record evidence before reconciliation.")
        if step.lease_until is not None and step.lease_until > timezone.now():
            return step
        _matches(plan.plan["records"], selected["target"])
        _validate(change, domain, source_client, target_client, registrar_client)
        token = uuid.uuid4()
        step.status, step.token, step.lease_until = "resolving", token, timezone.now() + timedelta(minutes=3)
        step.save(update_fields=["status", "token", "lease_until", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.dns.records_reconciliation_started",
            model_name="DomainDNSExecutionStep", object_id=str(step.pk), data={"observation_digest": observation_digest})
    try:
        # Read-only factual reconciliation does not renew old mutation consent.
        # Verify source/delegation around the complete target read.
        before = read_source_receipt(change, source_client, registrar_client)
        target = _receipt(change, target_client)
        after = read_source_receipt(change, source_client, registrar_client)
        _matches(plan.plan["records"], target)
        if (_fingerprint(target) != selected["target_digest"] or _fingerprint(before) != change.inventory_digest
                or _fingerprint(after) != change.inventory_digest):
            raise ValidationError("The inspected target or source delegation changed before reconciliation.")
        receipt = {"version": 1, "target": target, "plan_digest": plan.digest, "records_preparation_complete": True,
            "provider_action_complete": False, "original_submission_unproven": True,
            "delegation_updated": False, "public_activation_verified": False,
            "reconciled_observation_digest": observation_digest}
        with transaction.atomic():
            actor, domain, current = _lock(actor, change_id)
            _validate(current, domain, source_client, target_client, registrar_client)
            approved = approved_apply_evidence(current, domain)
            step = DomainDNSExecutionStep.objects.select_for_update().get(pk=step.pk)
            _intact(step, current)
            _lease(step, token)
            if approved.digest != plan.digest:
                raise ValidationError("The retained record consent changed during reconciliation.")
            step.status, step.completed_at = "complete", timezone.now()
            step.receipt, step.receipt_digest = receipt, _fingerprint(receipt)
            step.token = step.lease_until = None
            step.message = "Observed target records accepted. Original batch provenance remains unproven; delegation is pending."
            step.save(update_fields=["status", "completed_at", "receipt", "receipt_digest", "token", "lease_until", "message", "updated_at"])
            current.status = "records_ready"
            current.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.records_reconciled", model_name="DomainDNSExecutionStep",
                object_id=str(step.pk), data={"observation_digest": observation_digest, "provider_action_complete": False})
            return step
    except ERRORS:
        return _review(change_id, token)
