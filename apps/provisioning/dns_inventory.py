"""Lossless, twice-read WHM API 1 DNS zone inventory."""
import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import re

from django.core.exceptions import ValidationError
from apps.domains.dns_changes import backend_scope, configured_whm_nameservers
from apps.domains.registry_requirements import validate_nameservers
from .whm_client import WHMClientError


@dataclass(frozen=True)
class WHMZoneObservation:
    zone_id: str
    name: str
    nameservers: tuple[str, ...]
    status: str
    provider_scope: str
    serial: int


@dataclass(frozen=True)
class WHMDNSInventory:
    zone: WHMZoneObservation
    records: tuple[str, ...]
    digest: str
    unsupported_types: tuple[str, ...]


def _bytes(value):
    if type(value) is not str or len(value) > 16384:
        raise WHMClientError("WHM DNS binary data requires review.")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise WHMClientError("WHM DNS binary data requires review.") from None


def _owner(value, zone):
    raw = _bytes(value)
    try:
        text = raw.decode("ascii").lower()
    except UnicodeDecodeError:
        if raw.endswith(b".") and not raw.lower().endswith(("." + zone + ".").encode("ascii")):
            raise WHMClientError("The WHM zone response contains an unrelated binary owner.")
        return None
    if not text or len(text) > 254 or any(ord(c) < 33 or ord(c) == 127 for c in text):
        raise WHMClientError("WHM DNS owner data requires review.")
    name = text[:-1] if text.endswith(".") else text + "." + zone
    if name != zone and not name.endswith("." + zone):
        raise WHMClientError("The WHM zone response contains an unrelated owner.")
    return name


def _snapshot(payload, name, scope, configured):
    error = "The complete WHM zone response requires review."
    if type(payload) is not dict:
        raise WHMClientError(error)
    metadata, data = payload.get("metadata"), payload.get("data")
    if (type(metadata) is not dict or type(metadata.get("result")) is not int or metadata["result"] != 1
            or metadata.get("command") != "parse_dns_zone" or type(metadata.get("version")) is not int or metadata["version"] != 1
            or type(data) is not dict or type(data.get("payload")) is not list or not 1 <= len(data["payload"]) <= 2500):
        raise WHMClientError(error)
    lines, types, ns, soa, count = {}, set(), [], [], 0
    for row in data["payload"]:
        if type(row) is not dict or len(row) > 50:
            raise WHMClientError(error)
        index, kind = row.get("line_index"), row.get("type")
        if type(index) is not int or not 0 <= index <= 1000000 or index in lines:
            raise WHMClientError(error)
        if kind in ("comment", "control"):
            text = _bytes(row.get("text_b64"))
            if kind == "control":
                words = text.strip().split()
                if not ((len(words) == 2 and words[0] == b"$TTL" and re.fullmatch(rb"[0-9]{1,10}", words[1]))
                    or (len(words) == 2 and words[0] == b"$ORIGIN" and words[1].lower() == (name + ".").encode("ascii"))):
                    raise WHMClientError("WHM zone control statements require a reviewed inventory.")
        elif kind == "record":
            count += 1
            record_type, values = row.get("record_type"), row.get("data_b64")
            if (count > 1000 or type(record_type) is not str or re.fullmatch(r"[A-Z0-9]{1,20}", record_type) is None
                    or type(row.get("ttl")) is not int or not 0 <= row["ttl"] <= 2147483647
                    or type(values) is not list or not 1 <= len(values) <= 100):
                raise WHMClientError(error)
            owner = _owner(row.get("dname_b64"), name)
            decoded = [_bytes(value) for value in values]
            types.add(record_type)
            if owner is None or any(not value.isascii() for value in decoded):
                types.add("BINARY_DATA")
            if owner == name and record_type == "SOA":
                if len(decoded) != 7 or re.fullmatch(rb"[0-9]{1,10}", decoded[2]) is None or int(decoded[2]) > 4294967295:
                    raise WHMClientError(error)
                soa.append(int(decoded[2]))
            if owner == name and record_type == "NS":
                if len(decoded) != 1 or not decoded[0].isascii():
                    raise WHMClientError(error)
                host = decoded[0].decode("ascii").lower()
                ns.append(host[:-1] if host.endswith(".") else host + "." + name)
        else:
            raise WHMClientError(error)
        try:
            encoded = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise WHMClientError(error) from None
        if len(encoded.encode("utf-8")) > 16384:
            raise WHMClientError(error)
        lines[index] = encoded
    if len(soa) != 1 or not configured or set(ns) != set(configured):
        raise WHMClientError("WHM zone authority and configured nameservers require review.")
    try:
        validate_nameservers(name, ns)
    except ValidationError:
        raise WHMClientError("WHM delegation requires registry review.") from None
    zone = WHMZoneObservation(name, name, tuple(sorted(ns)), "configured", scope, soa[0])
    records = tuple(lines[index] for index in sorted(lines))
    digest = hashlib.sha256(json.dumps({"name": name, "scope": scope, "records": records}, sort_keys=True).encode()).hexdigest()
    return WHMDNSInventory(zone, records, digest, tuple(sorted(types - {"SOA", "A", "AAAA", "CNAME", "MX", "TXT", "SRV", "CAA", "NS"})))


def read_inventory(*, domain_name, client, expected_scope):
    from django.core.validators import validate_domain_name
    validate_domain_name(domain_name)
    try:
        name = domain_name.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise WHMClientError("The WHM DNS zone name requires review.") from None
    snapshots = []
    for _ in range(2):
        if backend_scope("cpanel", client) != expected_scope:
            raise WHMClientError("WHM configuration changed. Review this DNS request.")
        configured = configured_whm_nameservers(client)
        payload = client.parse_dns_zone(name)
        if backend_scope("cpanel", client) != expected_scope:
            raise WHMClientError("WHM configuration changed. Review this DNS request.")
        snapshots.append(_snapshot(payload, name, expected_scope, configured))
    if snapshots[0] != snapshots[1]:
        raise WHMClientError("The WHM DNS zone changed during inventory. Review a fresh inventory.")
    return snapshots[1]
