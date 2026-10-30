"""Offline, additive target-zone comparison. No implicit overwrites or deletes."""
from django.core.exceptions import ValidationError

from .contact_bindings import _fingerprint
from .dns_record_plan import build_record_plan


def _options(record):
    options = record.get("provider_options", {})
    settings = options.get("settings", {})
    return (options.get("proxied", False), settings.get("ipv4_only", False), settings.get("ipv6_only", False))


def _metadata(record):
    options = record.get("provider_options", {})
    tags, comment = options.get("tags", []), options.get("comment")
    if type(tags) is not list or any(type(tag) is not str for tag in tags) or (comment is not None and type(comment) is not str):
        raise ValidationError("The target DNS record metadata requires review.")
    return (comment or None, tuple(sorted(tags)))


def _groups(entries):
    groups = {}
    for entry in entries:
        if entry["disposition"] in {"copy", "duplicate"}:
            record = entry["record"]
            groups.setdefault((record["name"], record["type"]), []).append(entry)
    return groups


def build_target_plan(*, source_plan, target_receipt, target_digest):
    """Compare an intact source plan with a complete observed target inventory.

    Target absence must be explicit evidence supplied by a scoped discovery
    adapter. Missing, partial or unreadable target data never means absence.
    An exact RRset can be kept; a missing RRset can be created. Changed answers,
    TTLs or proxy behaviour require review, never an inferred replacement.
    """
    if type(source_plan) is not dict or type(target_receipt) is not dict:
        raise ValidationError("The retained DNS plans require review.")
    source = dict(source_plan)
    source_digest = source.pop("digest", None)
    if (source_digest != _fingerprint(source) or source.get("blocked") is not False
            or target_digest != _fingerprint(target_receipt)
            or target_receipt.get("provider") != source.get("target_provider")
            or target_receipt.get("name") != source.get("zone_name")
            or type(target_receipt.get("exists")) is not bool
            or type(target_receipt.get("scope")) is not str or len(target_receipt["scope"]) != 64):
        raise ValidationError("The retained DNS source plan or target identity requires review.")
    exists = target_receipt["exists"]
    if not exists:
        if target_receipt.get("zone") is not None or target_receipt.get("records") != []:
            raise ValidationError("The target absence evidence requires review.")
        inventory = {"zone": {"name": source["zone_name"]}, "records": []}
    else:
        inventory = {"zone": target_receipt.get("zone"), "records": target_receipt.get("records")}
    target = build_record_plan(source_provider=source["target_provider"], target_provider=source["target_provider"],
        zone_name=source["zone_name"], inventory_receipt=inventory, inventory_digest=_fingerprint(inventory))
    desired, existing = _groups(source["entries"]), _groups(target["entries"])
    owner_types = {}
    for name, kind in set(desired) | set(existing):
        owner_types.setdefault(name, set()).add(kind)
    actions, acknowledgements = [], set()
    for name, kind in sorted(set(desired) | set(existing)):
        before, after = existing.get((name, kind), []), desired.get((name, kind), [])
        action = {"name": name, "type": kind, "source_refs": [entry["source_ref"] for entry in after],
            "target_refs": [entry["source_ref"] for entry in before], "action": "blocked", "reason": ""}
        if "CNAME" in owner_types[name] and len(owner_types[name]) > 1:
            action["reason"] = "cname_conflict"
        elif not after:
            action.update(action="retain", reason="target_only_records")
            acknowledgements.add("retain_target_only_records")
        elif not before:
            action.update(action="create", reason="missing_target_rrset")
        else:
            left = {entry["record"]["rdata_wire"]: entry["record"] for entry in before}
            right = {entry["record"]["rdata_wire"]: entry["record"] for entry in after}
            if left.keys() != right.keys():
                action["reason"] = "rrset_answers_differ"
            elif any(entry["record"]["ttl"] != right[entry["record"]["rdata_wire"]]["ttl"] for entry in before):
                action["reason"] = "ttl_differs"
            elif any(_options(entry["record"]) != _options(right[entry["record"]["rdata_wire"]]) for entry in before):
                action["reason"] = "provider_behaviour_differs"
            else:
                action.update(action="keep", reason="matching_target_rrset")
                if any(_metadata(entry["record"]) != _metadata(right[entry["record"]["rdata_wire"]]) for entry in before):
                    acknowledgements.add("retain_target_metadata")
        actions.append(action)
    result = {"version": 1, "source_plan_digest": source_digest, "target_digest": target_digest,
        "provider": source["target_provider"], "zone_name": source["zone_name"], "target_exists": exists,
        "target_plan": target, "actions": actions,
        "blocked": target["blocked"] or any(action["action"] == "blocked" for action in actions),
        "required_acknowledgements": sorted(acknowledgements)}
    return {**result, "digest": _fingerprint(result)}
