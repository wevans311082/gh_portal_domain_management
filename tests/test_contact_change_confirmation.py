from unittest.mock import Mock

import pytest

from apps.domains.contact_change_confirmation import (
    ContactChangeEvidenceError, acknowledgement, identifier, irtp_state,
)
from apps.domains.resellerclub_client import ResellerClubClient


@pytest.mark.parametrize("value", [True, False, None, 0, -1, "01", " 12", "12 ", "1.2", "\u0661", "9" * 20, {}, []])
def test_identifiers_reject_ambiguous_values(value):
    assert identifier(value) == ""


def test_acknowledgement_retains_only_exact_identifiers():
    result = acknowledgement({"entityid": "123", "eaqid": 456, "message": "private contact details"}, order_id="123")
    assert result.order_id == "123"
    assert result.action_id == "456"
    assert "private" not in repr(result)


@pytest.mark.parametrize("payload", [{}, {"entityid": "124", "eaqid": "456"},
    {"entityid": "123", "eaqid": True}, {"entityid": "123", "eaqid": "456", "status": "ERROR"}])
def test_acknowledgement_rejects_unmatched_or_uncertain_response(payload):
    with pytest.raises(ContactChangeEvidenceError):
        acknowledgement(payload, order_id="123")


def evidence(state="SUCCESS"):
    return {"order-id": "123", "domain-name": "example.com", "irtp-status": {
        "task-status": state, "losing-foa-status": "APPROVED", "gaining-foa-status": "APPROVED"}}


@pytest.mark.parametrize("state", ["SUCCESS", "APPROVED", "PENDING", "EXPIRED", "REVOKED", "FAILED", "REMOTE_FAILURE"])
def test_irtp_preserves_execution_state(state):
    assert irtp_state(evidence(state), order_id="123", domain_name="example.com") == state


@pytest.mark.parametrize("field,value", [("order-id", "124"), ("domain-name", "foreign.com"), ("status", "ERROR")])
def test_irtp_rejects_foreign_order_or_error(field, value):
    payload = evidence()
    payload[field] = value
    with pytest.raises(ContactChangeEvidenceError):
        irtp_state(payload, order_id="123", domain_name="example.com")


@pytest.mark.parametrize("field,value", [("task-status", "Completed"), ("task-status", True),
    ("losing-foa-status", "PENDING"), ("gaining-foa-status", "DISAPPROVED"), ("gaining-foa-status", None)])
def test_irtp_rejects_unknown_or_conflicting_success(field, value):
    payload = evidence()
    payload["irtp-status"][field] = value
    with pytest.raises(ContactChangeEvidenceError):
        irtp_state(payload, order_id="123", domain_name="example.com")


def client():
    instance = object.__new__(ResellerClubClient)
    instance._post = Mock(return_value={})
    return instance


@pytest.mark.parametrize("domain", ["example.com", "example.co.uk", "example.io"])
def test_contact_payload_obeys_actual_extension_without_authorization_bypass(domain):
    api = client()
    api.change_domain_contacts("123", dict(reg="21", admin="22", tech="23", billing="24"), domain_name=domain)
    path, data = api._post.call_args.args
    assert path == "domains/modify-contact"
    assert data["designated-agent"] is False
    assert data["sixty-day-lock-optout"] is False
    assert "skip-irtp" not in data
    assert ("attr-name1" in data) == domain.endswith(".com")
    assert data["admin-contact-id"] == ("-1" if domain.endswith(".uk") else "22")


def test_invalid_role_cannot_reach_provider():
    api = client()
    with pytest.raises(ValueError):
        api.change_domain_contacts("123", dict(reg=True, admin="22", tech="23", billing="24"), domain_name="example.com")
    api._post.assert_not_called()


def test_irtp_lookup_uses_current_documented_post_contract():
    api = client()
    api.get_irtp_status("123")
    api._post.assert_called_once_with("domains/irtp/verification/details", {"order-id": "123"})
