from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError

from apps.domains.eligibility_contract import eligibility_observations, eligibility_request
from apps.domains.registrar_policy import policy_for
from apps.domains.resellerclub_client import ResellerClubClient


def test_asia_requires_both_documented_criteria_in_one_bounded_get():
    api = object.__new__(ResellerClubClient)
    api._get = Mock(return_value={"123": {"CED_ASIAN_COUNTRY": "Valid", "CED_DETAILS": "Valid"}})
    observations = api.get_registrant_eligibility_observations(["123"], tld="asia")
    api._get.assert_called_once_with("contacts/validate-registrant", {
        "contact-id": ["123"], "eligibility-criteria": ["CED_ASIAN_COUNTRY", "CED_DETAILS"]})
    assert len(observations) == 2 and all(item.status == "unknown" for item in observations)


@pytest.mark.parametrize("value", ["Success", "Valid", "Verified", "Failed", "Pending", "UnexpectedStatus"])
def test_unconfirmed_provider_status_vocabulary_never_becomes_approval(value):
    observations = eligibility_observations({"123": {"CPR": value}}, contact_ids=["123"], tld="ca")
    assert observations[0].provider_value == value
    assert observations[0].status == "unknown"


@pytest.mark.parametrize("payload", [{"124": {"CPR": "Valid"}}, {"123": {"APP_PREF_NEXUS": "Valid"}},
    {"123": {"CPR": True}}, {"123": {"CPR": "private provider message"}},
    {"status": "ERROR", "message": "private error"}, {"123": {"CPR": "Valid", "message": "private"}}])
def test_mismatched_criteria_contacts_and_untyped_results_require_review(payload):
    with pytest.raises(ValidationError):
        eligibility_observations(payload, contact_ids=["123"], tld="ca")


@pytest.mark.parametrize("ids", [[], [True], [" 123"], ["00123"], ["123", 123], list(range(1, 22)), "123"])
def test_invalid_requests_stop_before_provider_call(ids):
    api = object.__new__(ResellerClubClient)
    api._get = Mock()
    with pytest.raises(ValidationError):
        api.get_registrant_eligibility_observations(ids, tld="ca")
    api._get.assert_not_called()


def test_genuine_criteria_contract_does_not_enable_unimplemented_paid_policy():
    assert eligibility_request([123], "eu")["eligibility-criteria"] == ["EUROPEAN_COUNTRY"]
    with pytest.raises(ValidationError, match="eligibility review"):
        policy_for("eu")
    with pytest.raises(ValidationError, match="no reviewed"):
        eligibility_request([123], "com")
