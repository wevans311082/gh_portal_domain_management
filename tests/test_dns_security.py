from copy import deepcopy
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError

from apps.cloudflare_integration.scope import provider_scope
from apps.cloudflare_integration.services import CloudflareError
from apps.domains.dns_security import provider_dnssec, registrar_dnssec
from apps.provisioning.whm_client import WHMClient, WHMClientError
from tests.test_cloudflare_scope import provider
from tests.test_whm_dns_inventory import client_for
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_inventory_stage import admitted


def test_cloudflare_dnssec_transport_is_read_only_bounded_and_rejects_bad_identifiers():
    client = provider()
    response = Mock(status_code=200)
    response.json.return_value = {"success": True, "result": {"status": "disabled"}}
    with patch("apps.cloudflare_integration.services.requests.request", return_value=response) as http:
        assert client.get_dnssec("b" * 32)["result"]["status"] == "disabled"
        assert http.call_args.args[:2] == ("GET", client.BASE_URL + "/zones/" + "b" * 32 + "/dnssec")
        assert http.call_args.kwargs["timeout"] == (10, 30) and http.call_args.kwargs["allow_redirects"] is False
        with pytest.raises(CloudflareError):
            client.get_dnssec("../other")
        assert http.call_count == 1


@pytest.mark.parametrize("status", ["active", "pending", "pending-disabled", "error", None, False])
def test_cloudflare_signed_pending_and_unknown_states_require_separate_plan(status):
    client = provider()
    client.get_dnssec = Mock(return_value={"success": True, "result": {"status": status}})
    with pytest.raises(ValidationError):
        provider_dnssec(provider="cloudflare", name="example.com", zone_id="b" * 32, client=client, scope=provider_scope(client))


def test_cloudflare_safe_fact_contains_no_provider_body_and_rechecks_scope():
    client = provider()
    client.get_dnssec = Mock(return_value={"success": True, "result": {"status": "disabled", "PRIVATE": "never retain"}})
    scope = provider_scope(client)
    result = provider_dnssec(provider="cloudflare", name="example.com", zone_id="b" * 32, client=client, scope=scope)
    assert result["status"] == "disabled" and "PRIVATE" not in str(result)
    def changed(*args):
        client.account_id = "c" * 32
        return {"success": True, "result": {"status": "disabled"}}
    client.get_dnssec.side_effect = changed
    with pytest.raises(CloudflareError):
        provider_dnssec(provider="cloudflare", name="example.com", zone_id="b" * 32, client=client, scope=scope)


def whm_payload(records=None):
    return {"metadata": {"result": 1, "command": "fetch_ds_records_for_domains", "version": 1},
        "data": {"domains": [{"domain": "example.com", "ds_records": {} if records is None else records}]}}


@pytest.mark.parametrize("records,expected", [({}, "disabled"), ({"keys": {"123": {"privatekey": "SECRET"}}}, "configured")])
def test_whm_transport_never_returns_or_logs_dnssec_private_keys(records, expected, caplog):
    client = client_for()
    response = Mock(status_code=200)
    response.json.return_value = whm_payload(records)
    client.session.get = Mock(return_value=response)
    result = WHMClient.get_dnssec_status(client, "example.com")
    assert result == {"domain": "example.com", "status": expected}
    assert "SECRET" not in str(result) + caplog.text
    assert client.session.get.call_args.args == (client.base_url + "/fetch_ds_records_for_domains",)
    assert client.session.get.call_args.kwargs == {"params": {"api.version": 1, "domain": "example.com"},
        "timeout": (10, 30), "allow_redirects": False}


@pytest.mark.parametrize("change", ["error", "missing", "wrong-domain", "null-records", "wrong-command", "boolean-success"])
def test_whm_missing_unauthorized_or_malformed_dnssec_never_means_disabled(change):
    client = client_for()
    data = whm_payload()
    if change == "error":
        data["metadata"].update(result=0, reason="SECRET")
    elif change == "missing":
        data["data"]["domains"] = []
    elif change == "wrong-domain":
        data["data"]["domains"][0]["domain"] = "other.test"
    elif change == "null-records":
        data["data"]["domains"][0]["ds_records"] = None
    elif change == "wrong-command":
        data["metadata"]["command"] = "other"
    else:
        data["metadata"]["result"] = True
    response = Mock(status_code=200)
    response.json.return_value = data
    client.session.get = Mock(return_value=response)
    with pytest.raises(WHMClientError, match="requires review") as error:
        WHMClient.get_dnssec_status(client, "example.com")
    assert "SECRET" not in str(error.value)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("value", [None, "", False, 0, [{"keytag": "123"}], {"key": "signed"}])
def test_registrar_unknown_or_signed_dnssec_never_means_empty(admitted, value):
    registrar = admitted[3]
    payload = registrar.get_order_details("501")
    registrar.get_order_details.side_effect = lambda *args: {**deepcopy(payload), "dnssec": value}
    with pytest.raises(ValidationError):
        registrar_dnssec(source=admitted[0].source, client=registrar)


@pytest.mark.django_db(transaction=True)
def test_registrar_empty_collection_is_scoped_but_never_parent_ds_proof(admitted):
    registrar = admitted[3]
    payload = registrar.get_order_details("501")
    registrar.get_order_details.side_effect = lambda *args: {**deepcopy(payload), "dnssec": []}
    result = registrar_dnssec(source=admitted[0].source, client=registrar)
    assert result["ds_records"] == [] and result["parent_ds_verified"] is False
    registrar.get_order_details.side_effect = lambda *args: {**deepcopy(payload), "dnssec": [], "orders.dnssec": [{"keytag": "123"}]}
    with pytest.raises(ValidationError):
        registrar_dnssec(source=admitted[0].source, client=registrar)


@pytest.mark.parametrize("features", [{"dnssec_multi_signer": True}, {"dnssec_presigned": True}, {"dnssec_use_nsec3": 0}])
def test_cloudflare_dnssec_feature_state_cannot_be_guessed_from_disabled_status(features):
    client = provider()
    client.get_dnssec = Mock(return_value={"success": True, "result": {"status": "disabled", **features}})
    with pytest.raises(ValidationError, match="features"):
        provider_dnssec(provider="cloudflare", name="example.com", zone_id="b" * 32, client=client, scope=provider_scope(client))
