from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

import pytest
from django.db import connection

from apps.domains.models import DomainDNSChange, DomainDNSReservation
from apps.domains.dns_inventory import collect_dns_inventory
from apps.domains.resellerclub_client import ResellerClubClient
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_cloudflare_scope import provider
from tests.test_cloudflare_zones import zone
from tests.test_cloudflare_inventory import row, page
from tests.test_dns_switch_admission import preview, confirm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def admitted(current):
    domain = current[0]
    domain.dns_provider, domain.cloudflare_zone_id = "cloudflare", "b" * 32
    domain.set_nameserver_cache(["ns1.example.test", "ns2.example.test"])
    domain.save()
    source = provider()
    target = SimpleNamespace(host="whm.example.test", port=2087, username="root", api_token="PRIVATE-TOKEN",
        base_url="https://whm.example.test:2087/json-api", session=SimpleNamespace(verify=True, headers={"Authorization": "whm root:PRIVATE-TOKEN"}))
    change = preview(current, target_provider="cpanel", client=target, source_client=source)
    confirm(change, client=target, source_client=source)
    def details(*args, **kwargs):
        assert not connection.in_atomic_block
        return {"success": True, "result": zone(name=domain.name, status="active")}
    source.get_zone = Mock(side_effect=details)
    def records(*args, **kwargs):
        assert not connection.in_atomic_block
        return page([row(name=domain.name)])
    source.list_dns_records = Mock(side_effect=records)
    registrar = ResellerClubClient()
    payload = {"orderid": "501", "domainname": domain.name, "customerid": "999", "currentstatus": "Active",
        "registrantcontactid": "201", "admincontactid": "201", "techcontactid": "201", "billingcontactid": "201",
        "noOfNameServers": 2, "ns1": "ns1.example.test", "ns2": "ns2.example.test"}
    def registrar_details(*args, **kwargs):
        assert not connection.in_atomic_block
        return deepcopy(payload)
    registrar.get_order_details = Mock(side_effect=registrar_details)
    return change, source, target, registrar, domain


def run(admitted):
    change, source, target, registrar, domain = admitted
    return collect_dns_inventory(change.pk, source_client=source, target_client=target, registrar_client=registrar)


def test_read_stage_persists_full_receipt_and_replays_without_reads(admitted):
    result = run(admitted)
    assert result.status == "plan_review" and result.inventory_observed_at and len(result.inventory_digest) == 64
    assert len(result.inventory_receipt["records"]) == 1 and result.inventory_receipt["registrar_nameservers"] == ["ns1.example.test", "ns2.example.test"]
    assert run(admitted).status == "plan_review"
    assert admitted[1].get_zone.call_count == 2 and admitted[3].get_order_details.call_count == 2
    assert DomainDNSReservation.objects.filter(change=result).exists()
    admitted[4].refresh_from_db()
    assert admitted[4].dns_provider == "cloudflare" and result.zone_submitted_at is None


def test_active_lease_skips_duplicate_observation(admitted):
    from django.utils import timezone
    from datetime import timedelta
    DomainDNSChange.objects.filter(pk=admitted[0].pk).update(inventory_lease_until=timezone.now() + timedelta(minutes=5), inventory_token=uuid.uuid4())
    assert run(admitted).status == "inventory"
    admitted[1].get_zone.assert_not_called()


def test_wrong_authoritative_delegation_requires_review_and_retains_slot(admitted):
    admitted[3].get_order_details.side_effect = None
    admitted[3].get_order_details.return_value = {"PRIVATE": "INVALID"}
    result = run(admitted)
    assert result.status == "review" and not result.inventory_receipt
    assert "PRIVATE" not in result.inventory_message
    assert DomainDNSReservation.objects.filter(change=result).exists()


def test_source_change_during_read_discards_receipt(admitted):
    original = admitted[1].list_dns_records.side_effect
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        type(admitted[4]).objects.filter(pk=admitted[4].pk).update(cloudflare_zone_id="changed")
        return result
    admitted[1].list_dns_records.side_effect = changed
    result = run(admitted)
    assert result.status == "review" and not result.inventory_receipt


def test_target_configuration_changed_before_claim_prevents_http(admitted):
    admitted[2].api_token = "ROTATED"
    result = run(admitted)
    assert result.status == "review"
    admitted[1].get_zone.assert_not_called()


def test_old_observer_cannot_overwrite_a_new_lease(admitted):
    original = admitted[1].list_dns_records.side_effect
    replacement = uuid.uuid4()
    def replaced(*args, **kwargs):
        result = original(*args, **kwargs)
        DomainDNSChange.objects.filter(pk=admitted[0].pk).update(inventory_token=replacement)
        return result
    admitted[1].list_dns_records.side_effect = replaced
    result = run(admitted)
    assert result.status == "inventory" and result.inventory_token == replacement and not result.inventory_receipt


def test_revoked_actor_during_read_cannot_commit_evidence(admitted):
    original = admitted[1].list_dns_records.side_effect
    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        from apps.accounts.models import User
        User.objects.filter(pk=admitted[0].requested_by_id).update(is_active=False)
        return result
    admitted[1].list_dns_records.side_effect = revoked
    result = run(admitted)
    assert result.status == "review" and not result.inventory_receipt


def test_inventory_receipt_cannot_be_rewritten_through_model_save(admitted):
    result = run(admitted)
    result.inventory_receipt = {"replacement": True}
    from django.core.exceptions import ValidationError
    with pytest.raises(ValidationError, match="Retain the DNS inventory"):
        result.save()


def test_tampered_inventory_history_is_not_replayed_as_evidence(admitted):
    result = run(admitted)
    DomainDNSChange.objects.filter(pk=result.pk).update(inventory_receipt={"tampered": True})
    from django.core.exceptions import ValidationError
    with pytest.raises(ValidationError, match="inventory receipt"):
        run(admitted)


def test_whm_source_runs_shared_inventory_stage_for_cloudflare_target(current):
    from tests.test_whm_dns_inventory import client_for, payload as whm_payload
    domain = current[0]
    domain.dns_provider = "cpanel"
    domain.set_nameserver_cache(["ns1.example.test", "ns2.example.test"])
    domain.save()
    source, target = client_for(whm_payload(domain.name)), provider()
    raw = whm_payload(domain.name)
    def parsed(*args, **kwargs):
        assert not connection.in_atomic_block
        return deepcopy(raw)
    source.parse_dns_zone.side_effect = parsed
    change = preview(current, client=target, source_client=source)
    confirm(change, client=target, source_client=source)
    registrar = ResellerClubClient()
    def details(*args, **kwargs):
        assert not connection.in_atomic_block
        return {"orderid": "501", "domainname": domain.name, "customerid": "999", "currentstatus": "Active",
            "registrantcontactid": "201", "admincontactid": "201", "techcontactid": "201", "billingcontactid": "201",
            "noOfNameServers": 2, "ns1": "ns1.example.test", "ns2": "ns2.example.test"}
    registrar.get_order_details = Mock(side_effect=details)
    result = collect_dns_inventory(change.pk, source_client=source, target_client=target, registrar_client=registrar)
    assert result.status == "plan_review" and result.inventory_receipt["zone"]["status"] == "configured"
    assert len(result.inventory_receipt["records"]) == 8
    assert result.inventory_receipt["unsupported_types"] == ["BINARY_DATA", "HTTPS"]
    assert source.parse_dns_zone.call_count == 2 and registrar.get_order_details.call_count == 2
    assert DomainDNSReservation.objects.filter(change=change).exists()


@pytest.mark.parametrize("event", ["domain.dns.inventory_started", "domain.dns.inventory_observed"])
def test_audit_failure_preserves_claim_or_prevents_all_reads(admitted, event):
    from apps.audit.models import AuditLog
    create = AuditLog.objects.create
    def fail(*args, **kwargs):
        if kwargs.get("action") == event:
            raise RuntimeError("audit unavailable")
        return create(*args, **kwargs)
    with patch("apps.domains.dns_inventory.AuditLog.objects.create", side_effect=fail):
        with pytest.raises(RuntimeError):
            run(admitted)
    admitted[0].refresh_from_db()
    assert admitted[0].status == "inventory" and not admitted[0].inventory_receipt
    assert DomainDNSReservation.objects.filter(change=admitted[0]).exists()
    if event.endswith("started"):
        admitted[1].get_zone.assert_not_called()
        assert admitted[0].inventory_token is None
    else:
        assert admitted[0].inventory_token is not None
