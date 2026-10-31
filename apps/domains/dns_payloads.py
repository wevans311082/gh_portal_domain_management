"""Lossless provider payloads derived only from intact reviewed record plans."""
import base64
import json
import re

import dns.rdata
import dns.exception
from django.core.exceptions import ValidationError

from .contact_bindings import _fingerprint


def _rdata(record):
    try:
        wire = base64.b64decode(record["rdata_wire"], validate=True)
        value = dns.rdata.from_wire("IN", record["type"], wire, 0, len(wire))
        if value.to_wire(canonicalize=True) != wire:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, dns.exception.DNSException):
        raise ValidationError("The reviewed DNS wire answer cannot be encoded safely.") from None


def _parts(record):
    value, kind = _rdata(record), record["type"]
    if kind in {"A", "AAAA"}:
        return [value.address]
    if kind in {"CNAME", "NS"}:
        return [value.target.to_text()]
    if kind == "MX":
        return [str(value.preference), value.exchange.to_text()]
    if kind == "SRV":
        return [str(value.priority), str(value.weight), str(value.port), value.target.to_text()]
    try:
        if kind == "TXT":
            # The documented JSON API accepts strings, not arbitrary octets.
            # Never turn non-UTF8 bytes into replacement characters or Latin-1.
            return [part.decode("utf-8") for part in value.strings]
        if kind == "CAA":
            return [str(value.flags), value.tag.decode("ascii"), value.value.decode("utf-8")]
    except UnicodeError:
        raise ValidationError("This binary DNS answer needs a verified provider encoding before it can be written.") from None
    raise ValidationError("This record type has no approved provider-write encoding.")


def cloudflare_record(record):
    value, kind = _rdata(record), record["type"]
    body = {"name": record["name"].rstrip("."), "type": kind, "ttl": record["ttl"]}
    if kind in {"A", "AAAA"}:
        body["content"] = value.address
    elif kind in {"CNAME", "NS"}:
        body["content"] = value.target.to_text()
    elif kind == "MX":
        body.update(content=value.exchange.to_text(), priority=value.preference)
    elif kind == "TXT":
        body["content"] = value.to_text()
    elif kind == "SRV":
        body["data"] = dict(priority=value.priority, weight=value.weight, port=value.port, target=value.target.to_text())
    elif kind == "CAA":
        parts = _parts(record)
        body["data"] = dict(flags=value.flags, tag=parts[1], value=parts[2])
    else:
        raise ValidationError("This record type has no approved Cloudflare write encoding.")
    options = record.get("provider_options", {})
    body.update(proxied=options.get("proxied", False), settings=options.get("settings", {}), tags=options.get("tags", []))
    if options.get("comment") is not None:
        body["comment"] = options["comment"]
    return body


def compile_record_payload(plan):
    if type(plan) is not dict:
        raise ValidationError("Choose an intact, unblocked DNS application plan.")
    body = dict(plan)
    digest = body.pop("digest", None)
    if (digest != _fingerprint(body) or body.get("blocked") is not False
            or body.get("effect") != "records_only" or body.get("delegation_authorized") is not False
            or body.get("provider") not in {"cloudflare", "cpanel"}):
        raise ValidationError("Choose an intact, unblocked DNS application plan.")
    provider, removals, additions = body["provider"], [], []
    seen = set()
    for action in body["actions"]:
        if action["action"] not in {"create", "replace", "delete", "keep", "retain"}:
            raise ValidationError("Resolve every DNS action before encoding provider writes.")
        if action["action"] in {"replace", "delete"}:
            for entry in action["before"]:
                ref = entry["source_ref"]
                if ref in seen:
                    raise ValidationError("Each target DNS record can be removed only once.")
                seen.add(ref)
                if provider == "cloudflare" and re.fullmatch(r"cloudflare:[a-fA-F0-9]{32}", ref):
                    removals.append({"id": ref.split(":", 1)[1]})
                elif provider == "cpanel" and re.fullmatch(r"whm:[0-9]+", ref):
                    removals.append(int(ref.split(":", 1)[1]))
                else:
                    raise ValidationError("The target record identity does not match its provider.")
        if action["action"] in {"create", "replace"}:
            for entry in action["after"]:
                if entry["disposition"] == "duplicate":
                    continue
                record = entry["record"]
                if entry["disposition"] != "copy" or record["type"] in {"SOA"} or (
                        record["name"] == body["zone_name"] and record["type"] == "NS"):
                    raise ValidationError("Provider-managed authority is not an editable DNS action.")
                additions.append(cloudflare_record(record) if provider == "cloudflare" else {
                    "dname": record["name"], "ttl": record["ttl"], "record_type": record["type"], "data": _parts(record)})
    if provider == "cloudflare":
        if len(removals) + len(additions) > 3500:
            raise ValidationError("This record plan exceeds the provider's maximum single-batch capacity.")
        return {"deletes": removals, "posts": additions}
    serial = body["target"]["zone"].get("serial")
    if type(serial) is not int or not 0 <= serial <= 4294967295:
        raise ValidationError("WHM record changes require the exact observed SOA serial.")
    if len(removals) + len(additions) > 2500:
        raise ValidationError("This record plan exceeds the reviewed WHM batch capacity.")
    return {"zone": body["zone_name"].rstrip("."), "serial": serial,
            "remove": sorted(removals), "add": [json.dumps(row, ensure_ascii=True, separators=(",", ":")) for row in additions]}
