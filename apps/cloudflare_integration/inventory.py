"""Complete read-only DNS inventory; preserve unfamiliar records for review."""
from dataclasses import dataclass
import hashlib
import json
import re

from .scope import assert_provider_scope
from .services import CloudflareError
from .zones import observe_zone, ZoneObservation


@dataclass(frozen=True)
class DNSInventory:
    zone: ZoneObservation
    records: tuple[str, ...]
    digest: str
    unsupported_types: tuple[str, ...]


def _record(row, zone):
    error = "The complete Cloudflare DNS record evidence requires review."
    if type(row) is not dict or len(row) > 100:
        raise CloudflareError(error)
    identifier, name, kind = row.get("id"), row.get("name"), row.get("type")
    if (type(identifier) is not str or re.fullmatch(r"[a-fA-F0-9]{32}", identifier) is None
            or type(name) is not str or len(name) > 253 or any(ord(c) < 32 for c in name)
            or not (name.lower().rstrip(".") == zone.name or name.lower().rstrip(".").endswith("." + zone.name))
            or type(kind) is not str or re.fullmatch(r"[A-Z0-9]{1,20}", kind) is None
            or type(row.get("ttl")) is not int or not 1 <= row["ttl"] <= 2147483647
            or ("zone_id" in row and row["zone_id"] != zone.zone_id)
            or ("proxied" in row and type(row["proxied"]) is not bool)
            or ("priority" in row and (type(row["priority"]) is not int or not 0 <= row["priority"] <= 65535))
            or (kind == "MX" and "priority" not in row)
            or not (type(row.get("content")) is str or type(row.get("data")) is dict)):
        raise CloudflareError(error)
    try:
        encoded = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise CloudflareError(error) from None
    if len(encoded.encode("utf-8")) > 16384:
        raise CloudflareError(error)
    return identifier, kind, encoded


def read_inventory(*, zone_id, domain_name, client, expected_scope):
    zone = observe_zone(zone_id, domain_name=domain_name, client=client, expected_scope=expected_scope)
    records, types, pagination = {}, set(), None
    for page in range(1, 6):
        assert_provider_scope(expected_scope, client)
        payload = client.list_dns_records(zone.zone_id, page=page, per_page=200)
        assert_provider_scope(expected_scope, client)
        if type(payload) is not dict or payload.get("success") is not True:
            raise CloudflareError("Cloudflare DNS inventory was not confirmed.")
        rows, info = payload.get("result"), payload.get("result_info")
        if (type(rows) is not list or len(rows) > 200 or type(info) is not dict
                or any(type(info.get(key)) is not int for key in ("page", "count", "per_page", "total_pages", "total_count"))):
            raise CloudflareError("Cloudflare DNS inventory pagination is incomplete.")
        pages, size, total = info["total_pages"], info["per_page"], info["total_count"]
        if (info["page"] != page or info["count"] != len(rows) or not 1 <= size <= 200 or len(rows) > size
                or not 0 <= pages <= 5 or (pages == 0 and (page != 1 or rows)) or (pages > 0 and page > pages)
                or not 0 <= total <= 1000
                or (total > 0 and pages != (total + size - 1) // size)
                or (total == 0 and pages not in (0, 1))
                or len(rows) != max(0, min(size, total - (page - 1) * size))
                or (pagination is not None and pagination != (pages, size, total))):
            raise CloudflareError("Cloudflare DNS inventory pagination changed or exceeds the review limit.")
        pagination = (pages, size, total)
        for row in rows:
            identifier, kind, encoded = _record(row, zone)
            if identifier in records:
                raise CloudflareError("Cloudflare DNS inventory contains a repeated record. Review a fresh inventory.")
            records[identifier] = encoded
            types.add(kind)
        if page >= pages:
            break
    else:
        raise CloudflareError("Cloudflare DNS inventory exceeds the review limit.")
    if observe_zone(zone.zone_id, domain_name=zone.name, client=client, expected_scope=expected_scope) != zone:
        raise CloudflareError("The Cloudflare zone changed during inventory. Review a fresh inventory.")
    encoded_records = tuple(records[key] for key in sorted(records))
    digest = hashlib.sha256(json.dumps({"zone": zone.zone_id, "scope": expected_scope,
        "records": encoded_records}, sort_keys=True).encode()).hexdigest()
    return DNSInventory(zone, encoded_records, digest, tuple(sorted(types - {"A", "AAAA", "CNAME", "MX", "TXT", "SRV", "CAA", "NS"})))
