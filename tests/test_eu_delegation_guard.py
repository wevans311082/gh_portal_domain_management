from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError

from apps.domains.eu_registry import validate_eu_delegation_change
from tests.test_contact_change_admission import current
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


@pytest.fixture
def delegation(current, monkeypatch):
    from apps.domains import registrar_policy
    monkeypatch.setitem(registrar_policy.POLICIES, "eu", registrar_policy.EU)
    domain, source = current
    domain.name, domain.tld = "example.eu", "eu"
    domain.save()
    source.domain_name, source.tld = domain.name, domain.tld
    source.save()
    data = {"orderid": "501", "domainname": domain.name, "customerid": "999",
            "currentstatus": "Active", "registrantcontactid": "201", "noOfNameServers": "2",
            "ns1": "ns1.example.net", "ns2": "ns2.example.net"}
    client = Mock(get_registration_reconciliation_details=Mock(return_value=data))
    return domain, client, data


def test_guard_allows_reviewed_external_delegation_without_mutation(delegation):
    domain, client, data = delegation
    validate_eu_delegation_change(domain, ["ns1.example.net"], client=client)
    client.modify_nameservers.assert_not_called()


@pytest.mark.parametrize("case", ["existing_child", "proposed_child", "hidden_servers", "inconsistent_count", "foreign_order"])
def test_guard_blocks_child_or_incomplete_or_unrelated_delegation(delegation, case):
    domain, client, data = delegation
    proposed = ["ns1.example.net"]
    if case == "existing_child":
        data["ns2"] = "ns1.example.eu"
    elif case == "proposed_child":
        proposed = ["ns1.example.eu"]
    elif case == "hidden_servers":
        data.update(noOfNameServers="5", **{f"ns{i}": f"ns{i}.example.net" for i in range(1, 6)})
    elif case == "inconsistent_count":
        data["ns3"] = "ns3.example.net"
    else:
        data["customerid"] = "888"
    with pytest.raises(ValidationError):
        validate_eu_delegation_change(domain, proposed, client=client)
    client.modify_nameservers.assert_not_called()
