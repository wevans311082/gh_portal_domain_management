"""Leased read-only target discovery and conflict evidence for DNS switching."""
from dataclasses import asdict
from datetime import timedelta
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.cloudflare_integration.services import CloudflareError, CloudflareService
from apps.cloudflare_integration.zones import find_zone
from apps.cloudflare_integration.inventory import read_inventory as read_cloudflare_inventory
from apps.provisioning.whm_client import WHMClient, WHMClientError
from apps.provisioning.dns_inventory import read_inventory as read_whm_inventory
from .contact_bindings import _fingerprint
from .dns_changes import backend_scope, _manifest
from .dns_inventory import _locked
from .dns_plan_review import _fresh
from .dns_target_plan import build_target_plan
from .models import DomainDNSChange, DomainDNSRecordPlan, Domain
from .resellerclub_client import ResellerClubClient


def _approved(change):
    plan = DomainDNSRecordPlan.objects.select_for_update().filter(pk=change.approved_record_plan_id, change=change).first()
    if plan is None or plan.approved_at is None or plan.approved_by_id is None or plan.expires_at <= timezone.now():
        raise ValidationError("The approved source plan requires a fresh review.")
    body = dict(plan.plan)
    digest = body.pop("digest", None)
    if (digest != plan.digest or _fingerprint(body) != plan.digest or body.get("blocked") is not False
            or body.get("inventory_digest") != change.inventory_digest
            or body.get("target_provider") != change.target_provider):
        raise ValidationError("The retained source plan requires review.")
    return plan


def _receipt(change, client):
    return read_target_receipt(provider=change.target_provider, name=change.source["name"], scope=change.provider_scope, client=client)


def read_target_receipt(*, provider, name, scope, client):
    """Read one explicit authority; never rewrite a retained request's scope."""
    result = {"version": 1, "provider": provider, "name": name.lower().rstrip(".") + ".",
              "scope": scope, "exists": True}
    if provider == Domain.DNS_PROVIDER_CLOUDFLARE:
        zone = find_zone(domain_name=name, client=client, expected_scope=scope)
        if zone is None:
            # Repeat the complete scoped discovery. A transport/permission error
            # or one missing page must never be interpreted as an absent zone.
            if find_zone(domain_name=name, client=client, expected_scope=scope) is not None:
                raise ValidationError("The target zone appeared during discovery. Review a fresh request.")
            return {**result, "exists": False, "zone": None, "records": []}
        inventory = read_cloudflare_inventory(zone_id=zone.zone_id, domain_name=name, client=client,
            expected_scope=scope)
        if inventory.zone != zone or inventory.zone.paused:
            raise ValidationError("The target Cloudflare zone changed or is paused.")
    elif provider == Domain.DNS_PROVIDER_CPANEL:
        from apps.provisioning.dns_zones import zone_exists
        if not zone_exists(domain_name=name, client=client, expected_scope=scope):
            return {**result, "exists": False, "zone": None, "records": []}
        inventory = read_whm_inventory(domain_name=name, client=client, expected_scope=scope)
    else:
        raise ValidationError("This DNS target needs a reviewed adapter.")
    return {**result, "zone": asdict(inventory.zone), "records": list(inventory.records),
        "record_digest": inventory.digest, "unsupported_types": list(inventory.unsupported_types)}


def _review(change_id, token):
    # Keep the reservation after any unverified target result. This helper can
    # record a read failure even when the initiating actor was just revoked.
    from apps.accounts.models import User
    with transaction.atomic():
        identity = DomainDNSChange.objects.values("user_id", "domain_id").get(pk=change_id)
        User.objects.select_for_update().get(pk=identity["user_id"])
        Domain.objects.select_for_update().get(pk=identity["domain_id"])
        change = DomainDNSChange.objects.select_for_update().get(pk=change_id)
        if change.status != "target_inventory" or change.target_token != token:
            return change
        change.status = "review"
        change.target_token = change.target_lease_until = None
        change.target_message = "Target DNS could not be verified. Review the target account, zone and source plan before continuing."
        change.save(update_fields=["status", "target_token", "target_lease_until", "target_message", "updated_at"])
        AuditLog.objects.create(user_id=change.requested_by_id, action="domain.dns.target_review",
            model_name="DomainDNSChange", object_id=str(change.pk), data={})
        return change


def collect_target_inventory(change_id, *, source_client=None, target_client=None, registrar_client=None):
    if connection.in_atomic_block:
        raise ValidationError("Run target DNS inventory outside a database transaction.")
    if target_client is None:
        provider = DomainDNSChange.objects.values_list("target_provider", flat=True).get(pk=change_id)
        if provider not in (Domain.DNS_PROVIDER_CLOUDFLARE, Domain.DNS_PROVIDER_CPANEL):
            raise ValidationError("This DNS target needs a reviewed adapter.")
        target_client = CloudflareService() if provider == Domain.DNS_PROVIDER_CLOUDFLARE else WHMClient()
    registrar = registrar_client or ResellerClubClient()
    with transaction.atomic():
        actor, domain, change = _locked(change_id)
        if (change.manifest_digest != _manifest(change)
                or change.target_digest and change.target_digest != _fingerprint(change.target_receipt)):
            raise ValidationError("The retained target DNS evidence requires review.")
        if change.status not in ("plan_approved", "target_inventory"):
            return change
        if change.target_digest:
            raise ValidationError("Retain the target inventory. Review a new request for changed evidence.")
        if change.target_lease_until is not None and change.target_lease_until > timezone.now():
            return change
        try:
            _fresh(change, domain, source_client, target_client, registrar)
            plan = _approved(change)
            if change.zone_id or change.zone_observation:
                raise ValidationError("Target provider work needs reconciliation before inventory.")
        except (ValidationError, CloudflareError, WHMClientError):
            change.status = "review"
            change.target_token = change.target_lease_until = None
            change.target_message = "The source plan, target account or provider stage requires review before target inventory."
            change.save(update_fields=["status", "target_token", "target_lease_until", "target_message", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.target_review", model_name="DomainDNSChange",
                object_id=str(change.pk), data={})
            return change
        token = uuid.uuid4()
        change.target_token, change.target_lease_until = token, timezone.now() + timedelta(minutes=10)
        change.status = "target_inventory"
        change.save(update_fields=["target_token", "target_lease_until", "status", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.dns.target_started", model_name="DomainDNSChange",
            object_id=str(change.pk), data={"source_plan_id": plan.pk})
    try:
        target = _receipt(change, target_client)
        delta = build_target_plan(source_plan=plan.plan, target_receipt=target, target_digest=_fingerprint(target))
        receipt = {"version": 1, "source_plan_id": plan.pk, "source_plan_digest": plan.digest,
            "target": target, "comparison": delta}
        with transaction.atomic():
            actor, domain, current = _locked(change_id)
            if (current.status != "target_inventory" or current.target_token != token
                    or current.target_lease_until is None or current.target_lease_until <= timezone.now()):
                return current
            _fresh(current, domain, source_client, target_client, registrar)
            if _approved(current).pk != plan.pk or backend_scope(current.target_provider, target_client) != current.provider_scope:
                raise ValidationError("The reviewed source plan or target account changed.")
            current.target_receipt, current.target_digest = receipt, _fingerprint(receipt)
            current.target_observed_at = timezone.now()
            current.target_token = current.target_lease_until = None
            current.status = "target_review"
            current.target_message = ("Target records conflict with the source plan. Review before provider changes."
                if delta["blocked"] else "Target comparison retained. Explicit review is required before provider changes.")
            current.save(update_fields=["target_receipt", "target_digest", "target_observed_at", "target_token",
                "target_lease_until", "status", "target_message", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.target_observed", model_name="DomainDNSChange",
                object_id=str(current.pk), data={"source_plan_id": plan.pk, "exists": target["exists"], "blocked": delta["blocked"]})
            return current
    except (ValidationError, PermissionDenied, CloudflareError, WHMClientError):
        return _review(change_id, token)
