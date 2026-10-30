"""Deterministic, lossless DNS record planning. No network or provider writes."""
import base64
import binascii
import json
import re

import dns.exception
import dns.name
import dns.rdata
import dns.rdataclass
import dns.rdatatype
from dns.rdtypes.ANY.TXT import TXT
from django.core.exceptions import ValidationError

from .contact_bindings import _fingerprint

SUPPORTED = {"A", "AAAA", "CNAME", "MX", "TXT", "SRV", "CAA", "NS", "SOA"}
DNSSEC = {"DS", "DNSKEY", "RRSIG", "NSEC", "NSEC3", "NSEC3PARAM", "CDS", "CDNSKEY"}


def _decode(value):
    if type(value) is not str or len(value) > 16384:
        raise ValueError
    return base64.b64decode(value, validate=True)


def _quote(value):
    return '"' + "".join(chr(byte) if 32 <= byte <= 126 and byte not in (34, 92)
                         else "\\" + str(byte).zfill(3) for byte in value) + '"'


def _absolute(value):
    if type(value) is not str or value == "@":
        raise ValueError
    return dns.name.from_text(value, origin=dns.name.root).to_text()


def _cloudflare(row):
    kind, content = row["type"], row.get("content")
    if kind == "MX":
        if type(row.get("priority")) is not int:
            raise ValueError
        return str(row["priority"]) + " " + _absolute(content)
    if kind in {"CNAME", "NS"}:
        return _absolute(content)
    if kind == "SRV":
        data = row["data"]
        if any(type(data.get(key)) is not int for key in ("priority", "weight", "port")):
            raise ValueError
        return " ".join(str(data[key]) for key in ("priority", "weight", "port")) + " " + _absolute(data["target"])
    if kind == "CAA" and type(row.get("data")) is dict:
        data = row["data"]
        if type(data.get("flags")) is not int or type(data.get("tag")) is not str or type(data.get("value")) is not str:
            raise ValueError
        return f'{data["flags"]} {data["tag"]} ' + _quote(data["value"].encode("utf-8"))
    if type(content) is not str or (kind == "TXT" and not content.lstrip().startswith('"')):
        raise ValueError
    return content


def _entry(encoded, source, target, origin, index, ttl_overrides):
    entry = {"source_ref": str(index), "source": encoded, "disposition": "blocked", "reason": "invalid_record",
             "record": None, "acknowledgements": []}
    try:
        if type(encoded) is not str or len(encoded.encode("utf-8")) > 16384:
            raise ValueError
        row = json.loads(encoded)
        if type(row) is not dict:
            raise ValueError
        if source == "cpanel":
            if type(row.get("line_index")) is not int or row["line_index"] < 0:
                raise ValueError
            entry["source_ref"] = "whm:" + str(row["line_index"])
            if row.get("type") in {"comment", "control"}:
                _decode(row["text_b64"])
                entry.update(disposition="metadata", reason="retained_zone_metadata")
                return entry
            if row.get("type") != "record":
                raise ValueError
            kind = row["record_type"]
            owner = dns.name.from_text(_decode(row["dname_b64"]), origin=origin).canonicalize()
        else:
            if type(row.get("id")) is not str or re.fullmatch(r"[a-fA-F0-9]{32}", row["id"]) is None:
                raise ValueError
            entry["source_ref"] = "cloudflare:" + str(row["id"])
            kind = row["type"]
            owner = dns.name.from_text(row["name"], origin=dns.name.root).canonicalize()
        if type(kind) is not str or not owner.is_subdomain(origin) or any(not label.isascii() for label in owner.labels):
            raise ValueError
        if kind in DNSSEC:
            entry["reason"] = "dnssec_requires_separate_plan"
            return entry
        if kind not in SUPPORTED:
            entry["reason"] = "unsupported_record_type"
            return entry
        ttl = row["ttl"]
        if type(ttl) is not int or not 0 <= ttl <= 2147483647:
            raise ValueError
        override = ttl_overrides.get(entry["source_ref"])
        if override is not None:
            ttl = override
        entry["ttl_override"] = override
        if source == "cpanel":
            parts = [_decode(value) for value in row["data_b64"]]
            if kind == "TXT":
                rdata = TXT(dns.rdataclass.IN, dns.rdatatype.TXT, parts)
            else:
                text = (parts[0].decode("ascii") + " " + parts[1].decode("ascii") + " " + _quote(parts[2])) if kind == "CAA" and len(parts) == 3 else " ".join(part.decode("ascii") for part in parts)
                rdata = dns.rdata.from_text("IN", kind, text, origin=origin, relativize=False)
        else:
            rdata = dns.rdata.from_text("IN", kind, _cloudflare(row), origin=dns.name.root, relativize=False)
            if kind in {"SRV", "CAA"} and type(row.get("content")) is str:
                other = dns.rdata.from_text("IN", kind, row["content"], origin=dns.name.root, relativize=False)
                if other.to_wire(canonicalize=True) != rdata.to_wire(canonicalize=True):
                    raise ValueError
        wire = rdata.to_wire(origin=origin, canonicalize=True)
        entry["record"] = {"name": owner.to_text(), "type": kind, "ttl": ttl,
            "rdata_text": rdata.to_text(origin=origin, relativize=False), "rdata_wire": base64.b64encode(wire).decode("ascii")}
        settings = row.get("settings", {}) if source == "cloudflare" else {}
        if (type(settings) is not dict or set(settings) - {"ipv4_only", "ipv6_only"}
                or any(type(value) is not bool for value in settings.values())
                or (source == "cloudflare" and "proxied" in row and type(row["proxied"]) is not bool)):
            entry["reason"] = "provider_record_settings_require_review"
            return entry
        if source == "cloudflare" and row.get("private_routing") is not None and row["private_routing"] is not False:
            entry["reason"] = "private_routing_requires_separate_plan"
            return entry
        if target == "cloudflare":
            entry["record"]["provider_options"] = {"proxied": row.get("proxied", False) if source == "cloudflare" else False,
                "settings": settings, "comment": row.get("comment") if source == "cloudflare" else None,
                "tags": row.get("tags", []) if source == "cloudflare" else []}
        if owner == origin and kind in {"NS", "SOA"}:
            entry.update(disposition="provider_managed", reason="target_manages_apex_authority")
        elif kind == "SOA":
            entry["reason"] = "nested_soa_requires_review"
        elif target == "cpanel" and kind == "CNAME" and owner == origin:
            entry["reason"] = "apex_cname_requires_flattening"
        elif ttl == 0 or (target == "cpanel" and source == "cloudflare" and ttl == 1 and override is None) or (target == "cloudflare" and not (60 <= ttl <= 86400 or ttl == 1 and (source == "cloudflare" or override is not None))):
            entry["reason"] = "explicit_target_ttl_required"
        else:
            entry.update(disposition="copy", reason="preserve_dns_answer")
        if source == "cloudflare" and target == "cpanel" and row.get("proxied") is True:
            entry["acknowledgements"].append("cloudflare_proxy_removed")
        if source == "cloudflare" and target == "cpanel" and any(settings.values()):
            entry["acknowledgements"].append("cloudflare_record_settings_removed")
        if source == "cloudflare" and target == "cpanel" and (row.get("tags") or row.get("comment")):
            entry["acknowledgements"].append("provider_metadata_retained_in_history")
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, binascii.Error, RecursionError, dns.exception.DNSException):
        entry["disposition"], entry["reason"], entry["record"] = "blocked", "invalid_record", None
    return entry


def build_record_plan(*, source_provider, target_provider, zone_name, inventory_receipt, inventory_digest, ttl_overrides=None):
    ttl_overrides = {} if ttl_overrides is None else ttl_overrides
    if (type(ttl_overrides) is not dict or any(type(key) is not str or type(value) is not int
            or not 1 <= value <= 2147483647 for key, value in ttl_overrides.items())):
        raise ValidationError("Choose explicit valid TTL overrides for the reviewed records.")
    if (source_provider not in ("cloudflare", "cpanel") or target_provider not in ("cloudflare", "cpanel")
            or type(inventory_receipt) is not dict or inventory_digest != _fingerprint(inventory_receipt)):
        raise ValidationError("The retained DNS inventory requires review.")
    try:
        origin = dns.name.from_text(zone_name, origin=dns.name.root).canonicalize()
        zone = inventory_receipt["zone"]
        if dns.name.from_text(zone["name"], origin=dns.name.root).canonicalize() != origin:
            raise ValueError
        rows = inventory_receipt["records"]
        if type(rows) is not list or len(rows) > 2500:
            raise ValueError
    except (ValueError, KeyError, TypeError, dns.exception.DNSException):
        raise ValidationError("The retained DNS inventory identity requires review.") from None
    entries = [_entry(row, source_provider, target_provider, origin, index, ttl_overrides) for index, row in enumerate(rows)]
    if set(ttl_overrides) - {entry["source_ref"] for entry in entries if entry["record"] is not None
                             and entry["disposition"] not in {"metadata", "provider_managed"}}:
        raise ValidationError("TTL overrides must match editable records in this inventory.")
    groups, cnames, refs = {}, {}, set()
    for entry in entries:
        if entry["source_ref"] in refs:
            raise ValidationError("The retained DNS inventory contains repeated record identities.")
        refs.add(entry["source_ref"])
        record = entry["record"]
        if record is not None and entry["disposition"] == "copy":
            groups.setdefault((record["name"], record["type"]), []).append(entry)
            cnames.setdefault(record["name"], set()).add(record["type"])
    for (name, kind), group in groups.items():
        options = {}
        for entry in group:
            options.setdefault(entry["record"]["rdata_wire"], set()).add(_fingerprint(entry["record"].get("provider_options", {})))
        reason = ("cname_conflict" if "CNAME" in cnames[name] and len(cnames[name]) > 1 else
                  "rrset_ttl_conflict" if len({entry["record"]["ttl"] for entry in group}) > 1 else
                  "duplicate_provider_options_conflict" if any(len(values) > 1 for values in options.values()) else
                  "multiple_cname_targets" if kind == "CNAME" and len({entry["record"]["rdata_wire"] for entry in group}) > 1 else None)
        seen = {}
        for entry in group:
            if reason:
                entry.update(disposition="blocked", reason=reason)
            elif entry["record"]["rdata_wire"] in seen:
                entry.update(disposition="duplicate", reason="same_dns_answer", duplicate_of=seen[entry["record"]["rdata_wire"]])
            else:
                seen[entry["record"]["rdata_wire"]] = entry["source_ref"]
    plan = {"version": 1, "source_provider": source_provider, "target_provider": target_provider,
        "zone_name": origin.to_text(), "inventory_digest": inventory_digest, "entries": entries,
        "blocked": any(entry["disposition"] == "blocked" for entry in entries),
        "required_acknowledgements": sorted({value for entry in entries for value in entry["acknowledgements"]})}
    return {**plan, "digest": _fingerprint(plan)}
