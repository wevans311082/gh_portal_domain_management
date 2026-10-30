"""Read-only remote rechecks. These facts alone never authorize a mutation."""
from datetime import datetime, timedelta
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from .contact_bindings import _fingerprint
from .dns_inventory import _locked, read_source_receipt
from .dns_plan_review import _fresh
from .dns_security import provider_dnssec, registrar_dnssec
from .dns_target_inventory import _receipt
from .dns_target_review import target_evidence
from .dns_parent_ds import read_parent_ds


def _approved(change, domain, source_client, target_client, registrar):
    plan, comparison = target_evidence(change)
    if (change.status != "target_approved" or change.cancelled_at is not None
            or change.target_approved_at is None or change.target_approved_by_id is None
            or change.target_approved_digest != change.target_digest
            or change.target_acknowledgements != comparison["required_acknowledgements"]
            or comparison["blocked"] is not False or plan.expires_at <= timezone.now()
            or change.target_observed_at + timedelta(minutes=30) <= timezone.now()
            or change.zone_id or change.zone_observation or change.completed_at is not None):
        raise ValidationError("A current approved DNS target plan is required before execution checks.")
    _fresh(change, domain, source_client, target_client, registrar)


def observe_execution_inputs(change_id, *, source_client, target_client, registrar_client):
    """Re-read exact snapshots and scoped unsigned-provider/registrar facts.

    No zone creation, records or delegation changes; no job publication. Public
    parent DS denial, durable execution fencing and freshness at mutation time
    remain mandatory and deliberately cannot be inferred from these results.
    """
    if connection.in_atomic_block:
        raise ValidationError("Run DNS execution checks outside a database transaction.")
    with transaction.atomic():
        actor, domain, change = _locked(change_id)
        _approved(change, domain, source_client, target_client, registrar_client)
    retained_target = change.target_receipt["target"]
    def source_security():
        return provider_dnssec(provider=change.source["dns_provider"], name=change.source["name"],
            zone_id=change.inventory_receipt["zone"]["zone_id"], client=source_client, scope=change.source["dns_source_scope"])
    def target_security():
        if not retained_target["exists"]:
            return {"provider": change.target_provider, "status": "absent"}
        return provider_dnssec(provider=change.target_provider, name=change.source["name"],
            zone_id=retained_target["zone"]["zone_id"], client=target_client, scope=change.provider_scope)
    before = {"registrar": registrar_dnssec(source=change.source, client=registrar_client),
              "source": source_security(), "target": target_security()}
    parent_before = read_parent_ds(change.source["name"])
    source = read_source_receipt(change, source_client, registrar_client)
    target = _receipt(change, target_client)
    if _fingerprint(source) != change.inventory_digest or _fingerprint(target) != _fingerprint(retained_target):
        raise ValidationError("Source or target DNS changed after approval. Review a fresh request.")
    after = {"registrar": registrar_dnssec(source=change.source, client=registrar_client),
             "source": source_security(), "target": target_security()}
    parent_after = read_parent_ds(change.source["name"])
    if (before != after or before["registrar"]["nameservers"] != sorted(change.inventory_receipt["registrar_nameservers"])):
        raise ValidationError("DNSSEC configuration or delegation changed during execution checks.")
    if parent_before["parent_zone"] != parent_after["parent_zone"]:
        raise ValidationError("The public DNS parent changed during execution checks.")
    with transaction.atomic():
        actor, domain, current = _locked(change_id)
        _approved(current, domain, source_client, target_client, registrar_client)
        if current.target_digest != change.target_digest or current.inventory_digest != change.inventory_digest:
            raise ValidationError("The approved DNS evidence changed during execution checks.")
    if datetime.fromisoformat(parent_after["verified_until"]) <= timezone.now():
        raise ValidationError("The public parent DS proof expired during execution checks.")
    receipt = {"version": 1, "change_id": change.pk, "source_digest": change.inventory_digest,
        "target_digest": change.target_digest, "observed_at": timezone.now().isoformat(), "dnssec": after,
        "public_parent_ds": {"before": parent_before, "after": parent_after},
        "parent_ds_verified": True, "ds_transition_verified": False, "ready_for_mutation": False}
    return {**receipt, "digest": _fingerprint(receipt)}
