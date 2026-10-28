from unittest.mock import Mock

import pytest

from apps.domains.contact_change_confirmation import ContactChangeEvidenceError, action_observation
from apps.domains.resellerclub_client import ResellerClubClient


def payload(**overrides):
    return {"recsonpage": "1", "1": {"entityid": "501", "eaqid": "701", "actiontype": "ModifyContact",
        "actionstatus": "AwaitingIRTP", "actioncompleted": "0", **overrides}}


def observe(value):
    return action_observation(value, order_id="501", action_id="701")


def test_exact_action_observation_is_metadata_only():
    result = observe(payload(actiontypedesc="private description", message="private message"))
    assert result.order_id == "501" and result.action_id == "701"
    assert result.status == "AwaitingIRTP" and result.completed_at == ""
    assert "private" not in repr(result)


def test_dotted_fields_are_supported_without_alias_conflicts():
    data = payload()
    data["1"] = {"entityaction." + key: value for key, value in data["1"].items()}
    assert observe(data).action_id == "701"
    data["1"]["eaqid"] = "702"
    with pytest.raises(ContactChangeEvidenceError):
        observe(data)


@pytest.mark.parametrize("values", [{"entityid": "502"}, {"eaqid": "702"}, {"eaqid": True},
    {"actionstatus": ""}, {"actionstatus": "private provider error text"}, {"actioncompleted": True},
    {"actioncompleted": "-1"}, {"actiontype": {}}])
def test_foreign_or_malformed_records_are_rejected(values):
    with pytest.raises(ContactChangeEvidenceError):
        observe(payload(**values))


@pytest.mark.parametrize("value", [{}, {"status": "ERROR", "recsonpage": "0"}, {"recsonpage": False},
    {"recsonpage": "1", "1": {}, "2": {}}, {"recsonpage": "1", "1": []}])
def test_absence_is_not_inferred_from_error_or_ambiguous_result(value):
    with pytest.raises(ContactChangeEvidenceError):
        observe(value)


def test_exact_empty_record_count_means_no_matching_action():
    assert observe({"recsonpage": "0", "recsindb": "0"}) is None


def test_lookup_checks_archive_only_after_proven_current_absence():
    api = object.__new__(ResellerClubClient)
    api._get = Mock(side_effect=[{"recsonpage": "0"}, payload(actioncompleted="1793210000")])
    result = api.get_contact_change_action("501", "701")
    assert result.completed_at == "1793210000"
    assert [call.args[0] for call in api._get.call_args_list] == ["actions/search-current", "actions/search-archived"]
    assert api._get.call_args.args[1] == {"order-id": ["501"], "eaq-id": ["701"], "no-of-records": 2, "page-no": 1}


def test_foreign_current_result_does_not_fall_through_to_archive():
    api = object.__new__(ResellerClubClient)
    api._get = Mock(return_value=payload(entityid="502"))
    with pytest.raises(ContactChangeEvidenceError):
        api.get_contact_change_action("501", "701")
    assert api._get.call_count == 1
