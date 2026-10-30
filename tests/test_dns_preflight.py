from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.domains.dns_preflight import observe_execution_inputs
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_inventory_stage import admitted
from tests.test_dns_target_inventory import approved, read_method, cancel
from tests.test_dns_target_review import reviewed, approve

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def ready(reviewed, monkeypatch):
    approve(reviewed)
    return configure_unsigned_clients(reviewed, monkeypatch)


def configure_unsigned_clients(reviewed, monkeypatch):
    for client in reviewed[1:3]:
        if hasattr(client, "token"):
            def dnssec(*args):
                assert not connection.in_atomic_block
                return {"success": True, "result": {"status": "disabled"}}
            client.get_dnssec = Mock(side_effect=dnssec)
        else:
            def dnssec(name):
                assert not connection.in_atomic_block
                return {"domain": name, "status": "disabled"}
            client.get_dnssec_status = Mock(side_effect=dnssec)
    original = reviewed[3].get_order_details.side_effect
    def details(*args):
        return {**original(*args), "dnssec": []}
    reviewed[3].get_order_details.side_effect = details
    monkeypatch.setattr("apps.domains.dns_preflight.read_parent_ds", Mock(side_effect=lambda name: {
        "parent_zone": "com.", "name": name + ".", "state": "no_ds", "parent_ds_verified": True,
        "verified_until": (timezone.now() + timedelta(minutes=5)).isoformat()}))
    return reviewed


def observe(ready):
    return observe_execution_inputs(ready[0].pk, source_client=ready[1], target_client=ready[2], registrar_client=ready[3])


def test_preflight_rechecks_exact_snapshots_but_cannot_authorize_mutation(ready):
    before = ready[3].get_order_details.call_count
    result = observe(ready)
    assert result["source_digest"] == ready[0].inventory_digest and result["target_digest"] == ready[0].target_digest
    assert result["parent_ds_verified"] is True and result["ready_for_mutation"] is False
    assert result["ds_transition_verified"] is False
    assert ready[3].get_order_details.call_count == before + 4
    ready[0].refresh_from_db()
    assert ready[0].status == "target_approved" and ready[0].zone_submitted_at is None


def test_changed_source_records_require_fresh_review_even_with_same_delegation(ready):
    client = ready[1]
    method = client.list_dns_records if hasattr(client, "token") else client.parse_dns_zone
    original = method.side_effect
    def changed(*args, **kwargs):
        data = deepcopy(original(*args, **kwargs))
        if hasattr(client, "token"):
            data["result"][0]["ttl"] = 7200
        else:
            data["data"]["payload"][5]["ttl"] = 7200
        return data
    method.side_effect = changed
    with pytest.raises(ValidationError, match="changed after approval"):
        observe(ready)


def test_changed_target_records_require_fresh_review(ready):
    client = ready[2]
    method = client.list_dns_records if hasattr(client, "token") else client.parse_dns_zone
    original = method.side_effect
    def changed(*args, **kwargs):
        data = deepcopy(original(*args, **kwargs))
        if hasattr(client, "token"):
            data["result"][0]["priority"] = 20
        else:
            data["data"]["payload"][5]["data_b64"][0] = "MjA="
        return data
    method.side_effect = changed
    with pytest.raises(ValidationError, match="changed after approval"):
        observe(ready)


def test_dnssec_enabled_after_approval_blocks_preflight(ready):
    client = ready[1]
    if hasattr(client, "token"):
        client.get_dnssec.side_effect = lambda *args: {"success": True, "result": {"status": "active"}}
    else:
        client.get_dnssec_status.side_effect = lambda name: {"domain": name, "status": "configured"}
    with pytest.raises(ValidationError, match="separate reviewed"):
        observe(ready)


def test_cancelled_approval_history_is_not_execution_authority(ready):
    cancel(ready[0])
    before = ready[3].get_order_details.call_count
    with pytest.raises(ValidationError, match="current approved"):
        observe(ready)
    assert ready[3].get_order_details.call_count == before


def test_cancellation_during_read_rejects_all_returned_proofs(ready):
    original = ready[3].get_order_details.side_effect
    def cancelled(*args):
        result = original(*args)
        cancel(ready[0])
        return result
    ready[3].get_order_details.side_effect = cancelled
    with pytest.raises(ValidationError, match="current approved"):
        observe(ready)


def test_revoked_actor_during_read_cannot_receive_successful_proof(ready, django_user_model):
    original = ready[3].get_order_details.side_effect
    def revoked(*args):
        result = original(*args)
        django_user_model.objects.filter(pk=ready[0].requested_by_id).update(is_active=False)
        return result
    ready[3].get_order_details.side_effect = revoked
    with pytest.raises(PermissionDenied):
        observe(ready)


def test_preflight_cannot_hold_a_transaction_open_during_provider_reads(ready):
    with transaction.atomic():
        with pytest.raises(ValidationError, match="outside"):
            observe(ready)


def test_parent_ds_failure_cannot_be_replaced_with_empty_registrar_data(ready, monkeypatch):
    def unavailable(*args):
        raise ValidationError("Public parent DS could not be verified.")
    monkeypatch.setattr("apps.domains.dns_preflight.read_parent_ds", unavailable)
    with pytest.raises(ValidationError, match="Public parent DS"):
        observe(ready)


def test_parent_proof_expiry_before_return_requires_another_observation(ready, monkeypatch):
    monkeypatch.setattr("apps.domains.dns_preflight.read_parent_ds", lambda name: {
        "parent_zone": "com.", "verified_until": (timezone.now() - timedelta(seconds=1)).isoformat()})
    with pytest.raises(ValidationError, match="expired"):
        observe(ready)
