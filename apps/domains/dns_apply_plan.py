"""Exact, explicitly resolved record changes; no provider calls or delegation."""
from copy import deepcopy

from django.core.exceptions import ValidationError

from .contact_bindings import _fingerprint
from .dns_target_plan import build_target_plan, _groups, _metadata, _options


def build_apply_plan(*, source_plan, target_receipt, target_digest, decisions=None):
    """Resolve whole RRsets against complete target evidence.

    Replacement always means the complete reviewed source RRset; it never
    merges answers or accepts client-supplied record bodies. Deleting target-only
    records requires a separate exact decision. Unsupported or inconsistent
    inventories stay blocked, regardless of decisions.
    """
    comparison = build_target_plan(source_plan=source_plan, target_receipt=target_receipt, target_digest=target_digest)
    if target_receipt["exists"] is not True:
        raise ValidationError("Prepare and verify the target zone before reviewing record changes.")
    decisions = [] if decisions is None else decisions
    if type(decisions) is not list or len(decisions) > 2500:
        raise ValidationError("Choose explicit record replacements or deletions from this inventory.")
    desired = _groups(source_plan["entries"])
    existing = _groups(comparison["target_plan"]["entries"])
    choices = {}
    for choice in decisions:
        if (type(choice) is not dict or set(choice) != {"name", "type", "action"}
                or any(type(value) is not str for value in choice.values())
                or choice["action"] not in {"replace", "delete"}):
            raise ValidationError("Choose an exact record replacement or deletion.")
        key = choice["name"], choice["type"]
        if (key in choices or key not in existing or
                (choice["action"] == "replace") != (key in desired)):
            raise ValidationError("Record decisions must identify distinct editable target RRsets and their source counterparts.")
        choices[key] = choice["action"]

    actions, acknowledgements, final = [], set(), []
    # The raw before/after entries include every duplicate and original identity.
    # Final DNS answers are deduplicated, without removing unreviewed target rows.
    for old in comparison["actions"]:
        key = old["name"], old["type"]
        before, after = existing.get(key, []), desired.get(key, [])
        if old["reason"] == "cname_conflict" and before and after:
            old = dict(old)
            left = {entry["record"]["rdata_wire"]: entry["record"] for entry in before}
            right = {entry["record"]["rdata_wire"]: entry["record"] for entry in after}
            if left.keys() != right.keys():
                old["reason"] = "rrset_answers_differ"
            elif any(entry["record"]["ttl"] != right[entry["record"]["rdata_wire"]]["ttl"] for entry in before):
                old["reason"] = "ttl_differs"
            elif any(_options(entry["record"]) != _options(right[entry["record"]["rdata_wire"]]) for entry in before):
                old["reason"] = "provider_behaviour_differs"
            else:
                old.update(action="keep", reason="matching_target_rrset")
        selected = choices.get(key)
        if selected == "replace" and old["action"] == "keep":
            raise ValidationError("Matching target RRsets do not require replacement.")
        action, reason = (selected, "explicit_source_replacement" if selected == "replace" else "explicit_target_deletion") if selected else (old["action"], old["reason"])
        if action == "replace":
            acknowledgements.add("replace_target_records")
        elif action == "delete":
            acknowledgements.add("delete_target_records")
        elif action == "retain":
            acknowledgements.add("retain_target_only_records")
        elif action == "keep":
            right = {entry["record"]["rdata_wire"]: entry["record"] for entry in after}
            if any(_metadata(entry["record"]) != _metadata(right[entry["record"]["rdata_wire"]]) for entry in before):
                acknowledgements.add("retain_target_metadata")
        action_body = {"name": key[0], "type": key[1], "action": action, "reason": reason,
                       "before": deepcopy(before), "after": deepcopy(after) if action != "delete" else []}
        # CNAME conflicts are evaluated below against the complete proposed final
        # zone, after all explicit target-only deletions have been applied.
        if action == "blocked" and reason == "cname_conflict":
            if not before and after:
                action_body.update(action="create", reason="missing_target_rrset")
            elif before and not after:
                action_body.update(action="retain", reason="target_only_records")
                acknowledgements.add("retain_target_only_records")
        rows = before if action_body["action"] in {"retain", "keep"} else after if action_body["action"] in {"create", "replace"} else []
        seen = set()
        for entry in rows:
            record = entry["record"]
            fingerprint = _fingerprint(record)
            if fingerprint not in seen:
                seen.add(fingerprint)
                final.append(deepcopy(record))
        actions.append(action_body)
    owner_types = {}
    for record in final:
        owner_types.setdefault(record["name"], set()).add(record["type"])
    # Apex SOA/NS do not enter editable groups, but still prohibit apex CNAME.
    for entry in comparison["target_plan"]["entries"]:
        if entry["disposition"] == "provider_managed":
            owner_types.setdefault(entry["record"]["name"], set()).add(entry["record"]["type"])
    conflicts = {name for name, kinds in owner_types.items() if "CNAME" in kinds and len(kinds) > 1}
    for action in actions:
        if action["name"] in conflicts:
            action.update(action="blocked", reason="cname_conflict")
    body = {"version": 1, "effect": "records_only", "source_plan_digest": source_plan["digest"],
            "target_digest": target_digest, "provider": comparison["provider"], "zone_name": comparison["zone_name"],
            "target": deepcopy(target_receipt), "target_plan": comparison["target_plan"],
            "decisions": [dict(name=name, type=kind, action=action) for (name, kind), action in sorted(choices.items())],
            "actions": actions, "final_records": final,
            "blocked": comparison["target_plan"]["blocked"] or any(action["action"] == "blocked" for action in actions),
            "required_acknowledgements": sorted(acknowledgements),
            "delegation_authorized": False}
    return {**body, "digest": _fingerprint(body)}
