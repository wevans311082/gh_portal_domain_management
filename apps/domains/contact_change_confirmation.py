"""Strict, metadata-only evidence at the contact-change acknowledgement boundary.

An acknowledgement means accepted for processing, never completed. Unknown
provider fields and statuses must not become authorization or local success.
"""

import re
from dataclasses import dataclass


def identifier(value):
    value = str(value) if type(value) in (str, int) else ""
    return value if re.fullmatch(r"[1-9][0-9]{0,18}", value) else ""


class ContactChangeEvidenceError(ValueError):
    def __init__(self):
        super().__init__("Registrar contact-change evidence requires review.")


@dataclass(frozen=True)
class ContactChangeAcknowledgement:
    order_id: str
    action_id: str


@dataclass(frozen=True)
class ContactChangeAction:
    order_id: str
    action_id: str
    action_type: str
    status: str
    completed_at: str


def action_observation(payload, *, order_id, action_id):
    """Read a bounded legacy action search without trusting search filters.

    Status codes are retained as observations, not interpreted as completion.
    The public contract does not enumerate their values. Conflicting flattened
    aliases, extra rows and missing IDs require review instead of first-row wins.
    """
    if (not identifier(order_id) or not identifier(action_id) or not isinstance(payload, dict)
            or len(payload) > 12 or str(payload.get("status", "")).upper() == "ERROR"):
        raise ContactChangeEvidenceError()
    rows = [value for key, value in payload.items() if str(key).isascii() and str(key).isdigit()]
    if len(rows) > 1:
        raise ContactChangeEvidenceError()
    if not rows:
        # Empty results need explicit record-count evidence, never an error body.
        if type(payload.get("recsonpage")) not in (str, int) or str(payload["recsonpage"]) != "0":
            raise ContactChangeEvidenceError()
        return None
    row = rows[0]
    if not isinstance(row, dict) or len(row) > 40:
        raise ContactChangeEvidenceError()
    fields = {}
    allowed = {"entityid", "eaqid", "actiontype", "actionstatus", "actioncompleted"}
    for key, value in row.items():
        name = str(key).rsplit(".", 1)[-1]
        if name in allowed:
            if name in fields and fields[name] != value:
                raise ContactChangeEvidenceError()
            fields[name] = value
    if identifier(fields.get("entityid")) != identifier(order_id) or identifier(fields.get("eaqid")) != identifier(action_id):
        raise ContactChangeEvidenceError()
    for name in ("actiontype", "actionstatus"):
        value = fields.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
            raise ContactChangeEvidenceError()
    completed = fields.get("actioncompleted")
    if completed in (None, "", "0", 0) and type(completed) is not bool:
        completed = ""
    elif not identifier(completed):
        raise ContactChangeEvidenceError()
    else:
        completed = identifier(completed)
    return ContactChangeAction(identifier(order_id), identifier(action_id), fields["actiontype"], fields["actionstatus"], completed)


def acknowledgement(payload, *, order_id):
    """Retain only matched order/action IDs; discard messages and contact PII."""
    if (not identifier(order_id) or not isinstance(payload, dict)
            or identifier(payload.get("entityid")) != identifier(order_id)
            or not identifier(payload.get("eaqid"))
            or str(payload.get("status", "")).upper() == "ERROR"):
        raise ContactChangeEvidenceError()
    return ContactChangeAcknowledgement(identifier(order_id), identifier(payload["eaqid"]))


def irtp_state(payload, *, order_id, domain_name):
    """APPROVED is still pending execution; only SUCCESS can support completion.

This evidence must additionally be paired with the exact acknowledged action
and live provider contact IDs. The endpoint alone is not action-specific.
"""
    if (not identifier(order_id) or not isinstance(payload, dict)
            or identifier(payload.get("order-id")) != identifier(order_id)
            or payload.get("domain-name") != domain_name
            or str(payload.get("status", "")).upper() == "ERROR"):
        raise ContactChangeEvidenceError()
    details = payload.get("irtp-status")
    if not isinstance(details, dict):
        raise ContactChangeEvidenceError()
    state = details.get("task-status")
    if not isinstance(state, str) or state not in {
        "PENDING", "REVOKED", "EXPIRED", "FAILED", "APPROVED", "SUCCESS", "REMOTE_FAILURE",
    }:
        raise ContactChangeEvidenceError()
    for field in ("losing-foa-status", "gaining-foa-status"):
        if details.get(field) not in {"PENDING", "APPROVED", "DISAPPROVED"}:
            raise ContactChangeEvidenceError()
    if state == "SUCCESS" and any(details[field] != "APPROVED" for field in ("losing-foa-status", "gaining-foa-status")):
        raise ContactChangeEvidenceError()
    return state
