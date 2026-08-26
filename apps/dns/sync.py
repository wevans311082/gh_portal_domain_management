"""Import and push DNS records to WHM, ResellerClub, or Cloudflare."""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.dns.models import DNSRecord, DNSZone
from apps.domains.models import Domain

logger = logging.getLogger(__name__)


def ensure_zone(domain: Domain) -> DNSZone:
    zone = getattr(domain, "dns_zone", None)
    if zone:
        return zone
    provider = domain.dns_provider or Domain.DNS_PROVIDER_CPANEL
    return DNSZone.objects.create(domain=domain, provider=provider, is_active=True)


def import_records(zone: DNSZone) -> int:
    provider = (zone.provider or zone.domain.dns_provider or "").lower()
    created = 0
    try:
        if provider in {"cpanel", "whm"}:
            created = _import_whm(zone)
        elif provider in {"registrar", "resellerclub"}:
            created = _import_resellerclub(zone)
        elif provider == "cloudflare":
            created = _import_cloudflare(zone)
    except Exception as exc:
        logger.warning("DNS import failed for %s: %s", zone.domain.name, exc)
    zone.last_synced = timezone.now()
    zone.save(update_fields=["last_synced", "updated_at"])
    return created


def push_record(zone: DNSZone, record: DNSRecord, action: str = "create"):
    provider = (zone.provider or zone.domain.dns_provider or "").lower()
    try:
        if provider in {"cpanel", "whm"}:
            _push_whm(zone, record, action)
        elif provider in {"registrar", "resellerclub"}:
            _push_resellerclub(zone, record, action)
        elif provider == "cloudflare":
            _push_cloudflare(zone, record, action)
    except Exception as exc:
        logger.error("DNS push failed action=%s record=%s: %s", action, record.pk, exc)
        raise


def _import_whm(zone: DNSZone) -> int:
    from apps.provisioning.whm_client import WHMClient

    raw_records = WHMClient().dump_zone(zone.domain.name)
    created = 0
    for raw in raw_records:
        record_type = str(raw.get("type") or raw.get("record_type") or "").upper()
        if record_type not in dict(DNSRecord.RECORD_TYPES):
            continue
        name = str(raw.get("name") or raw.get("dname") or "@").rstrip(".")
        content = str(
            raw.get("address")
            or raw.get("cname")
            or raw.get("txtdata")
            or raw.get("exchange")
            or raw.get("nsdname")
            or raw.get("record")
            or ""
        )
        if not content:
            continue
        line = str(raw.get("line") or raw.get("Line") or "")
        ttl = raw.get("ttl") or 3600
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            ttl = 3600
        obj, was_created = DNSRecord.objects.get_or_create(
            zone=zone,
            record_type=record_type,
            name=name,
            content=content,
            defaults={
                "ttl": ttl,
                "external_id": line,
                "priority": _safe_int(raw.get("preference") or raw.get("priority")),
                "is_active": True,
            },
        )
        if was_created:
            created += 1
        elif line and not obj.external_id:
            obj.external_id = line
            obj.save(update_fields=["external_id"])
    return created


def _import_resellerclub(zone: DNSZone) -> int:
    from apps.domains.resellerclub_client import ResellerClubClient

    order_id = zone.domain.registrar_id
    if not order_id:
        return 0
    raw_records = ResellerClubClient().list_dns_records(order_id)
    created = 0
    for raw in raw_records:
        record_type = str(raw.get("type") or raw.get("record_type") or "").upper()
        if record_type not in dict(DNSRecord.RECORD_TYPES):
            continue
        name = str(raw.get("host") or raw.get("name") or "@")
        content = str(raw.get("value") or raw.get("content") or raw.get("rdata") or "")
        if not content:
            continue
        obj, was_created = DNSRecord.objects.get_or_create(
            zone=zone,
            record_type=record_type,
            name=name,
            content=content,
            defaults={
                "ttl": _safe_int(raw.get("ttl"), 3600) or 3600,
                "priority": _safe_int(raw.get("priority") or raw.get("mx_priority")),
                "is_active": True,
            },
        )
        if was_created:
            created += 1
    return created


def _import_cloudflare(zone: DNSZone) -> int:
    from apps.cloudflare_integration.services import CloudflareService

    cf_id = getattr(zone.domain, "cloudflare_zone_id", "") or ""
    if not cf_id:
        return 0
    cf = CloudflareService()
    payload = cf.list_dns_records(cf_id) if hasattr(cf, "list_dns_records") else None
    records = []
    if isinstance(payload, dict):
        records = payload.get("result") or []
    elif isinstance(payload, list):
        records = payload
    created = 0
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record_type = str(raw.get("type") or "").upper()
        if record_type not in dict(DNSRecord.RECORD_TYPES):
            continue
        obj, was_created = DNSRecord.objects.get_or_create(
            zone=zone,
            record_type=record_type,
            name=str(raw.get("name") or "@"),
            content=str(raw.get("content") or ""),
            defaults={
                "ttl": _safe_int(raw.get("ttl"), 3600) or 3600,
                "proxied": bool(raw.get("proxied")),
                "external_id": str(raw.get("id") or ""),
                "is_active": True,
            },
        )
        if was_created:
            created += 1
    return created


def _push_whm(zone: DNSZone, record: DNSRecord, action: str):
    from apps.provisioning.whm_client import WHMClient

    client = WHMClient()
    domain = zone.domain.name
    if action == "create":
        client.add_zone_record(domain, record.name, record.record_type, record.content, ttl=record.ttl)
    elif action == "update" and record.external_id:
        try:
            line = int(record.external_id)
        except (TypeError, ValueError):
            return
        client.edit_zone_record(domain, line, record.name, record.record_type, record.content, ttl=record.ttl)
    elif action == "delete" and record.external_id:
        try:
            line = int(record.external_id)
        except (TypeError, ValueError):
            return
        client.remove_zone_record(domain, line)


def _push_resellerclub(zone: DNSZone, record: DNSRecord, action: str):
    from apps.domains.resellerclub_client import ResellerClubClient

    order_id = zone.domain.registrar_id
    if not order_id:
        return
    client = ResellerClubClient()
    host = record.name
    if action == "create":
        client.add_dns_record(order_id, host, record.content, record.record_type, ttl=record.ttl)
    elif action == "update":
        previous = record.content
        client.update_dns_record(order_id, host, previous, record.content, record.record_type, ttl=record.ttl)
    elif action == "delete":
        client.delete_dns_record(order_id, host, record.content, record.record_type)


def _push_cloudflare(zone: DNSZone, record: DNSRecord, action: str):
    from apps.cloudflare_integration.services import CloudflareService

    cf = CloudflareService()
    zone_cf_id = getattr(zone.domain, "cloudflare_zone_id", "") or ""
    if not zone_cf_id:
        return
    if action == "create":
        result = cf.create_dns_record(
            zone_cf_id,
            record_type=record.record_type,
            name=record.name,
            content=record.content,
            ttl=record.ttl,
            proxied=record.proxied,
        )
        record.external_id = (result or {}).get("result", {}).get("id", "") if isinstance(result, dict) else ""
        if record.external_id:
            record.save(update_fields=["external_id"])
    elif action == "update" and record.external_id:
        cf.update_dns_record(
            zone_cf_id,
            record.external_id,
            type=record.record_type,
            name=record.name,
            content=record.content,
            ttl=record.ttl,
            proxied=record.proxied,
        )
    elif action == "delete" and record.external_id:
        cf.delete_dns_record(zone_cf_id, record.external_id)


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
