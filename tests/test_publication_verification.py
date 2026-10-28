from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError

from apps.domains.publication_verification import publication_observation
from apps.domains.resellerclub_client import ResellerClubClient


def payload(consent="false"):
    return {"entityid": "501", "domainname": "example.com", "customerid": "999",
        "registrantOrgConsentDetails": {"isRegistrantOrgConsentApplicable": "true", "registrantOrgConsent": consent}}


def observe(value):
    return publication_observation(value, order_id="501", domain_name="example.com", customer_id="999")


@pytest.mark.parametrize("value,expected", [("false", False), ("true", True), (None, None), ("<null>", None)])
def test_documented_consent_values_remain_distinct(value, expected):
    result = observe(payload(value))
    assert result.applicable is True and result.consent is expected


@pytest.mark.parametrize("value", [True, False, "False", "0", 0, {}, [], "", "pending"])
def test_ambiguous_consent_cannot_confirm_revocation(value):
    with pytest.raises(ValidationError):
        observe(payload(value))


@pytest.mark.parametrize("key,value", [("entityid", "502"), ("domainname", "foreign.com"), ("customerid", "888"),
    ("entityid", True), ("status", "ERROR"), ("registrantOrgConsentDetails", None)])
def test_foreign_or_error_publication_evidence_is_rejected(key, value):
    data = payload()
    data[key] = value
    with pytest.raises(ValidationError):
        observe(data)


def test_missing_consent_is_not_equivalent_to_revocation():
    data = payload()
    del data["registrantOrgConsentDetails"]["registrantOrgConsent"]
    with pytest.raises(ValidationError):
        observe(data)


def test_conflicting_order_aliases_require_review():
    data = payload()
    data["orderid"] = "501"
    data["entityid"] = "502"
    with pytest.raises(ValidationError):
        observe(data)


def test_reader_requests_documented_contact_details_without_mutation():
    api = object.__new__(ResellerClubClient)
    api._get = Mock(return_value=payload())
    assert api.get_publication_reconciliation_details("501")["registrantOrgConsentDetails"]["registrantOrgConsent"] == "false"
    path, parameters = api._get.call_args.args
    assert path == "domains/details"
    assert parameters["order-id"] == "501" and "RegistrantContactDetails" in parameters["options"]
