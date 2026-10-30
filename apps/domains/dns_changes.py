"""Durable DNS switch preview/confirmation; no provider writes or job publishing."""
import json
import re
import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.accounts.models import User
from apps.admin_tools.access import is_owner
from apps.audit.models import AuditLog
from .models import Domain, DomainDNSChange, DomainDNSReservation
from .contact_bindings import _fingerprint


def configured_whm_nameservers(client):
    from apps.core.runtime_settings import get_runtime_setting
    from django.core.validators import validate_domain_name
    values = getattr(client, "dns_nameservers", None)
    if values is None:
        values = get_runtime_setting("WHM_NAMESERVERS", [])
    if type(values) is str:
        values = [value for value in re.split(r"[,\s]+", values.strip()) if value]
    if type(values) not in (list, tuple) or len(values) > 13 or any(type(value) is not str for value in values):
        raise ValidationError("WHM nameserver configuration requires owner review.")
    normalized = [value.strip().lower().rstrip(".") for value in values]
    for value in normalized:
        validate_domain_name(value)
    if len(set(normalized)) != len(normalized):
        raise ValidationError("WHM nameserver configuration requires owner review.")
    return tuple(normalized)


def zone_creation_options(target, client=None):
    if target != Domain.DNS_PROVIDER_CPANEL:
        return {}
    from apps.core.runtime_settings import get_runtime_setting
    from ipaddress import IPv4Address
    value = getattr(client, "dns_zone_ip", None)
    if value is None:
        value = get_runtime_setting("WHM_DNS_ZONE_IP", "")
    if value == "":
        return {}
    try:
        if type(value) is not str:
            raise ValueError
        ip = str(IPv4Address(value))
    except ValueError:
        raise ValidationError("WHM zone creation requires an owner-reviewed IPv4 address.") from None
    return {"ip": ip, "template": "simple", "owner": "authenticated_user", "nameservers": list(configured_whm_nameservers(client))}


def backend_scope(target, client=None):
    if target == Domain.DNS_PROVIDER_CLOUDFLARE:
        from apps.cloudflare_integration.services import CloudflareService
        from apps.cloudflare_integration.scope import provider_scope
        return provider_scope(client or CloudflareService())
    if target != Domain.DNS_PROVIDER_CPANEL:
        raise ValidationError("Choose Cloudflare DNS or WHM/platform DNS.")
    from apps.provisioning.whm_client import WHMClient
    from urllib.parse import urlsplit
    client = client or WHMClient()
    token, username = client.api_token, client.username
    if (type(token) is not str or not token or len(token) > 4096 or any(ord(c) <= 32 or ord(c) > 126 for c in token)
            or type(username) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", username) is None
            or type(client.port) is not int or not 1 <= client.port <= 65535
            or type(client.host) is not str or not client.host
            or client.base_url != f"https://{client.host}:{client.port}/json-api"
            or client.session.verify is not True
            or client.session.headers.get("Authorization") != f"whm {username}:{token}"):
        raise ValidationError("WHM DNS configuration requires owner review.")
    try:
        parsed = urlsplit(client.base_url)
        from apps.admin_tools.setting_schema import validate_setting
        validate_setting("WHM_HOST", client.host)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path != "/json-api" or parsed.port != client.port):
            raise ValueError
    except ValueError:
        raise ValidationError("WHM DNS configuration requires owner review.") from None
    return salted_hmac("domains.dns.whm.provider_scope.v1", json.dumps({"endpoint": client.base_url,
        "username": username, "credential": token, "nameservers": configured_whm_nameservers(client)}, sort_keys=True), algorithm="sha256").hexdigest()


def _context(actor, domain_id):
    if type(domain_id) is not int or domain_id <= 0:
        raise PermissionDenied
    identity = Domain.objects.filter(pk=domain_id).values("user_id").first()
    if identity is None:
        raise PermissionDenied
    customer = User.objects.select_for_update().get(pk=identity["user_id"])
    current_actor = User.objects.get(pk=actor.pk)
    if not current_actor.is_active or not (is_owner(current_actor) or current_actor.pk == customer.pk and not current_actor.is_staff and not current_actor.is_superuser):
        raise PermissionDenied
    domain = Domain.objects.select_for_update().get(pk=domain_id)
    if (domain.user_id != customer.pk or not customer.is_active or customer.is_staff or customer.is_superuser):
        raise PermissionDenied
    return current_actor, domain


def _source(domain, *, dns_change_id=None, source_client=None):
    from .bulk_reservations import assert_local_domain_editable
    from .registrar_review import assert_registrar_review_complete
    from .contact_changes import locked_source
    from .registry_requirements import policy_snapshot
    from apps.dns.models import DNSZone
    assert_local_domain_editable(domain, dns_change_id=dns_change_id)
    assert_registrar_review_complete(domain, dns_change_id=dns_change_id)
    if domain.status != Domain.STATUS_ACTIVE:
        raise ValidationError("DNS switching requires an active domain.")
    source, attempt, kind = locked_source(domain)
    zone = DNSZone.objects.select_for_update().filter(domain=domain).first()
    records = list(zone.records.select_for_update().order_by("pk").values("id", "record_type", "name", "content",
        "ttl", "priority", "proxied", "external_id", "is_active")[:1001]) if zone else []
    if len(records) > 1000 or any(len(row["content"]) > 8192 for row in records):
        raise ValidationError("This DNS record inventory requires owner review before switching providers.")
    return {"domain_id": domain.pk, "owner_id": domain.user_id, "name": domain.name, "status": domain.status,
        "registrar_id": domain.registrar_id, "registrar_scope": attempt.provider_scope, "customer_id": attempt.customer_id,
        "source_kind": kind, "source_id": source.pk, "remote_contacts": source.registrar_contact_ids,
        "policy": policy_snapshot(domain.name), "dns_provider": domain.dns_provider,
        "cloudflare_zone_id": domain.cloudflare_zone_id, "nameservers": domain.cached_nameservers,
        "dns_source_scope": backend_scope(domain.dns_provider, source_client)
            if domain.dns_provider in (Domain.DNS_PROVIDER_CLOUDFLARE, Domain.DNS_PROVIDER_CPANEL) else None,
        "zone": {"id": zone.pk, "provider": zone.provider, "is_active": zone.is_active} if zone else None,
        "cached_records": records, "inventory_verified": False}


def _manifest(change):
    body = {"key": str(change.key), "domain_id": change.domain_id, "user_id": change.user_id, "actor_id": change.requested_by_id,
        "target": change.target_provider, "scope": change.provider_scope, "source": change.source,
        "request_digest": change.request_digest, "expires_at": change.expires_at.isoformat()}
    if getattr(change, "zone_creation", {}):
        body["zone_creation"] = change.zone_creation
    return _fingerprint(body)


@transaction.atomic
def create_dns_preview(*, actor, domain_id, target_provider, key, client=None, source_client=None):
    if type(target_provider) is not str or target_provider not in (Domain.DNS_PROVIDER_CLOUDFLARE, Domain.DNS_PROVIDER_CPANEL):
        raise ValidationError("Choose Cloudflare DNS or WHM/platform DNS.")
    try:
        key = uuid.UUID(str(key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Use a valid DNS switch request key.") from None
    actor, domain = _context(actor, domain_id)
    identity = _fingerprint({"domain_id": domain.pk, "user_id": domain.user_id, "actor_id": actor.pk, "target": target_provider})
    previous = DomainDNSChange.objects.select_for_update().filter(key=key).first()
    if previous:
        if previous.request_digest != identity or previous.manifest_digest != _manifest(previous):
            raise ValidationError("This DNS request key is already used or requires review. Start a new preview.")
        return previous
    source = _source(domain, source_client=source_client)
    change = DomainDNSChange(key=key, domain=domain, user_id=domain.user_id, requested_by=actor,
        target_provider=target_provider, provider_scope=backend_scope(target_provider, client), source=source,
        zone_creation=zone_creation_options(target_provider, client),
        request_digest=identity, expires_at=timezone.now() + timedelta(minutes=30))
    change.manifest_digest = _manifest(change)
    change.save()
    AuditLog.objects.create(user=actor, action="domain.dns.previewed", model_name="DomainDNSChange", object_id=str(change.pk),
        data={"domain_id": domain.pk, "target_provider": target_provider})
    return change


@transaction.atomic
def confirm_dns_change(*, actor, change_id, manifest_digest, confirmed, client=None, source_client=None):
    if confirmed is not True:
        raise ValidationError("Explicitly confirm the reviewed DNS provider switch.")
    identity = DomainDNSChange.objects.filter(pk=change_id, requested_by_id=actor.pk).values("domain_id").first()
    if identity is None:
        raise PermissionDenied
    actor, domain = _context(actor, identity["domain_id"])
    change = DomainDNSChange.objects.select_for_update().get(pk=change_id)
    if (type(manifest_digest) is not str or not constant_time_compare(manifest_digest, change.manifest_digest)
            or change.manifest_digest != _manifest(change)):
        raise ValidationError("The DNS preview changed. Review a new preview.")
    if change.status == "cancelled" or change.cancelled_at is not None:
        raise ValidationError("This DNS request was cancelled. Review a new preview.")
    if change.confirmed_at is not None:
        return change
    if change.status != "draft":
        raise ValidationError("This DNS request is not awaiting confirmation.")
    if change.expires_at <= timezone.now():
        raise ValidationError("The DNS preview expired. Review a new preview.")
    if (_source(domain, source_client=source_client) != change.source or backend_scope(change.target_provider, client) != change.provider_scope
            or zone_creation_options(change.target_provider, client) != change.zone_creation):
        raise ValidationError("The DNS source or provider configuration changed. Review a new preview.")
    DomainDNSReservation.objects.create(domain=domain, change=change)
    change.confirmed_at = timezone.now()
    change.status = "inventory"
    change.save(update_fields=["confirmed_at", "status", "updated_at"])
    AuditLog.objects.create(user=actor, action="domain.dns.confirmed", model_name="DomainDNSChange", object_id=str(change.pk),
        data={"domain_id": domain.pk, "target_provider": change.target_provider})
    # The next stage must obtain complete live inventory and a reviewed record
    # plan. Cached records never authorize provider mutation or delegation.
    return change
