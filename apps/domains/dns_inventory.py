"""Leased read-only inventory stage; no DNS or registrar mutations."""
from dataclasses import asdict
from datetime import timedelta
import uuid
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.cloudflare_integration.inventory import read_inventory
from apps.cloudflare_integration.services import CloudflareError, CloudflareService
from .bulk_observation import observed_nameservers
from .contact_bindings import _fingerprint, provider_scope as registrar_scope
from .dns_changes import _context, _source, _manifest, backend_scope
from .models import DomainDNSChange, DomainDNSReservation, Domain
from .resellerclub_client import ResellerClubClient, ResellerClubError
from apps.provisioning.whm_client import WHMClientError


def _locked(change_id):
    identity = DomainDNSChange.objects.values("domain_id", "requested_by_id").get(pk=change_id)
    actor, domain = _context(SimpleNamespace(pk=identity["requested_by_id"]), identity["domain_id"])
    return actor, domain, DomainDNSChange.objects.select_for_update().get(pk=change_id)


def _validate(change, domain, source_client, target_client, registrar):
    if (change.confirmed_at is None or change.user_id != domain.user_id
            or change.manifest_digest != _manifest(change)
            or not DomainDNSReservation.objects.filter(change=change, domain=domain).exists()):
        raise ValidationError("The retained DNS request or its reservation requires review.")
    if (_source(domain, dns_change_id=change.pk, source_client=source_client) != change.source
            or backend_scope(change.target_provider, target_client) != change.provider_scope
            or registrar_scope(registrar) != change.source["registrar_scope"]):
        raise ValidationError("The DNS source or account configuration changed.")


def _delegation(change, registrar):
    if registrar_scope(registrar) != change.source["registrar_scope"]:
        raise ValidationError("The registrar account changed.")
    payload = registrar.get_order_details(change.source["registrar_id"])
    if registrar_scope(registrar) != change.source["registrar_scope"]:
        raise ValidationError("The registrar account changed.")
    return tuple(sorted(observed_nameservers(payload, change.source)))


def _review(change_id, token):
    # Recording a read failure never frees the slot or resets a mutation marker.
    with transaction.atomic():
        identity = DomainDNSChange.objects.values("user_id", "domain_id").get(pk=change_id)
        from apps.accounts.models import User
        User.objects.select_for_update().get(pk=identity["user_id"])
        Domain.objects.select_for_update().get(pk=identity["domain_id"])
        change = DomainDNSChange.objects.select_for_update().get(pk=change_id)
        if change.inventory_token != token or change.status != "inventory":
            return change
        change.status = "review"
        change.inventory_message = "DNS inventory could not be verified. Review the source, configuration and delegation before continuing."
        change.inventory_token = change.inventory_lease_until = None
        change.save(update_fields=["status", "inventory_message", "inventory_token", "inventory_lease_until", "updated_at"])
        AuditLog.objects.create(user_id=change.requested_by_id, action="domain.dns.inventory_review", model_name="DomainDNSChange", object_id=str(change.pk), data={})
        return change


def collect_dns_inventory(change_id, *, source_client=None, target_client=None, registrar_client=None):
    if connection.in_atomic_block:
        raise ValidationError("Run DNS inventory outside a database transaction.")
    if source_client is None:
        source_provider = DomainDNSChange.objects.values_list("source", flat=True).get(pk=change_id).get("dns_provider")
        if source_provider == Domain.DNS_PROVIDER_CPANEL:
            from apps.provisioning.whm_client import WHMClient
            source_client = WHMClient()
        else:
            source_client = CloudflareService()
    registrar = registrar_client or ResellerClubClient()
    with transaction.atomic():
        actor, domain, change = _locked(change_id)
        if (change.manifest_digest != _manifest(change) or (change.inventory_digest
                and change.inventory_digest != _fingerprint(change.inventory_receipt))):
            raise ValidationError("The retained DNS request or inventory receipt requires review.")
        if change.status != "inventory":
            return change
        if change.inventory_lease_until is not None and change.inventory_lease_until > timezone.now():
            return change
        try:
            _validate(change, domain, source_client, target_client, registrar)
        except (ValidationError, CloudflareError):
            change.status = "review"
            change.inventory_message = "The DNS source, reservation or configuration requires review before inventory can run."
            change.save(update_fields=["status", "inventory_message", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.inventory_review", model_name="DomainDNSChange", object_id=str(change.pk), data={})
            return change
        token = uuid.uuid4()
        change.inventory_token, change.inventory_lease_until = token, timezone.now() + timedelta(minutes=10)
        change.save(update_fields=["inventory_token", "inventory_lease_until", "updated_at"])
        AuditLog.objects.create(user=actor, action="domain.dns.inventory_started", model_name="DomainDNSChange", object_id=str(change.pk), data={})
    try:
        before = _delegation(change, registrar)
        if change.source["dns_provider"] == Domain.DNS_PROVIDER_CLOUDFLARE:
            inventory = read_inventory(zone_id=change.source["cloudflare_zone_id"], domain_name=change.source["name"],
                client=source_client, expected_scope=change.source["dns_source_scope"])
            expected_status = "active"
        elif change.source["dns_provider"] == Domain.DNS_PROVIDER_CPANEL:
            from apps.provisioning.dns_inventory import read_inventory as read_whm_inventory
            inventory = read_whm_inventory(domain_name=change.source["name"], client=source_client,
                expected_scope=change.source["dns_source_scope"])
            expected_status = "configured"
        else:
            raise ValidationError("This DNS source needs a reviewed inventory adapter.")
        after = _delegation(change, registrar)
        if (inventory.zone.status != expected_status or before != after
                or before != tuple(sorted(inventory.zone.nameservers))):
            raise ValidationError("The DNS source is not the verified authoritative delegation.")
        receipt = {"version": 1, "zone": asdict(inventory.zone), "records": list(inventory.records),
            "record_digest": inventory.digest, "unsupported_types": list(inventory.unsupported_types),
            "registrar_nameservers": list(after)}
        with transaction.atomic():
            actor, domain, current = _locked(change_id)
            if (current.status != "inventory" or current.inventory_token != token
                    or current.inventory_lease_until is None or current.inventory_lease_until <= timezone.now()):
                return current
            _validate(current, domain, source_client, target_client, registrar)
            current.inventory_receipt, current.inventory_digest = receipt, _fingerprint(receipt)
            current.inventory_observed_at = timezone.now()
            current.inventory_token = current.inventory_lease_until = None
            current.status = "plan_review"
            current.inventory_message = "Source inventory retained. Review the migration plan before provider changes."
            current.save(update_fields=["inventory_receipt", "inventory_digest", "inventory_observed_at", "inventory_token",
                "inventory_lease_until", "status", "inventory_message", "updated_at"])
            AuditLog.objects.create(user=actor, action="domain.dns.inventory_observed", model_name="DomainDNSChange", object_id=str(current.pk), data={"record_count": len(inventory.records)})
            return current
    except (ValidationError, PermissionDenied, CloudflareError, ResellerClubError, WHMClientError):
        return _review(change_id, token)
