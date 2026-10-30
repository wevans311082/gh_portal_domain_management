from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.dns_changes import create_dns_preview
from apps.domains.dns_controls import cancel_dns_change
from apps.domains.dns_inventory import collect_dns_inventory
from apps.domains.dns_plan_review import create_record_plan, approve_record_plan
from apps.domains.dns_target_inventory import collect_target_inventory
from apps.domains.models import DomainDNSChange, DomainDNSReservation
from apps.provisioning.whm_client import WHMClientError
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_inventory_stage import admitted
from tests.test_dns_switch_admission import confirm
from tests.test_cloudflare_scope import provider
from tests.test_cloudflare_zones import zone, page as zone_page
from tests.test_cloudflare_inventory import row, page as record_page
from tests.test_whm_dns_inventory import client_for, payload

pytestmark = pytest.mark.django_db(transaction=True)


def cancel(change):
    return cancel_dns_change(actor=change.requested_by, change_id=change.pk, manifest_digest=change.manifest_digest,
        confirmed=True, reason="user_cancelled")


@pytest.fixture(params=["cpanel", "cloudflare"])
def approved(admitted, request):
    return build_approved(admitted, request.param)


def build_approved(admitted, target_provider):
    old, source, target, registrar, domain = admitted
    cancel(old)
    raw = payload(domain.name)
    raw["data"]["payload"] = raw["data"]["payload"][:6]
    if target_provider == "cpanel":
        target.dns_nameservers = ("ns1.example.test", "ns2.example.test")
        def listed(*args):
            assert not connection.in_atomic_block
            return {"metadata": {"result": 1, "version": 1, "command": "listzones"}, "data": {"zone": [{"domain": domain.name}]}}
        target.list_dns_zones = Mock(side_effect=listed)
        for item in raw["data"]["payload"]:
            if item["type"] == "record":
                item["ttl"] = 3600
        def parsed(*args):
            assert not connection.in_atomic_block
            return deepcopy(raw)
        target.parse_dns_zone = Mock(side_effect=parsed)
    else:
        domain.dns_provider, domain.cloudflare_zone_id = "cpanel", ""
        domain.save()
        source, target = client_for(raw), provider()
        parsed = source.parse_dns_zone.side_effect
        def source_parsed(*args):
            assert not connection.in_atomic_block
            return parsed(*args)
        source.parse_dns_zone.side_effect = source_parsed
        def zones(*args, **kwargs):
            assert not connection.in_atomic_block
            return zone_page([zone(name=domain.name)])
        def detail(*args, **kwargs):
            assert not connection.in_atomic_block
            return {"success": True, "result": zone(name=domain.name)}
        def records(*args, **kwargs):
            assert not connection.in_atomic_block
            return record_page([row(name=domain.name, ttl=14400)])
        target.list_zones, target.get_zone, target.list_dns_records = Mock(side_effect=zones), Mock(side_effect=detail), Mock(side_effect=records)
    change = create_dns_preview(actor=old.requested_by, domain_id=domain.pk, target_provider=target_provider,
        key=uuid.uuid4(), client=target, source_client=source)
    confirm(change, client=target, source_client=source)
    collect_dns_inventory(change.pk, source_client=source, target_client=target, registrar_client=registrar)
    values = dict(source_client=source, target_client=target, registrar_client=registrar)
    plan = create_record_plan(actor=change.requested_by, change_id=change.pk, **values)
    approve_record_plan(actor=change.requested_by, plan_id=plan.pk, digest=plan.digest, confirmed=True,
        acknowledgements=plan.plan["required_acknowledgements"], **values)
    return change, source, target, registrar, domain


def run(approved):
    return collect_target_inventory(approved[0].pk, source_client=approved[1], target_client=approved[2], registrar_client=approved[3])


def read_method(approved):
    return approved[2].parse_dns_zone if approved[0].target_provider == "cpanel" else approved[2].list_zones


def test_target_snapshot_and_comparison_are_durable_and_replayed_without_reads(approved):
    result = run(approved)
    assert result.status == "target_review" and result.target_observed_at and result.target_digest
    assert not result.target_receipt["comparison"]["blocked"]
    assert result.target_receipt["comparison"]["actions"][0]["action"] == "keep"
    count = read_method(approved).call_count
    assert run(approved).target_digest == result.target_digest and read_method(approved).call_count == count
    assert result.zone_submitted_at is None and not result.zone_id and not result.zone_observation
    assert DomainDNSReservation.objects.filter(change=result).exists()
    assert approved[3].get_order_details.call_count == 2  # Source inventory only; no registrar update.


def test_target_conflict_is_retained_for_review_without_mutation(approved):
    if approved[0].target_provider == "cpanel":
        data = read_method(approved).side_effect("example.com")
        data["data"]["payload"][5]["data_b64"][0] = "MjA="  # MX priority 20.
        read_method(approved).side_effect = lambda *args: deepcopy(data)
    else:
        approved[2].list_dns_records.side_effect = lambda *args, **kwargs: record_page([row(name=approved[4].name, priority=20, ttl=14400)])
    result = run(approved)
    assert result.status == "target_review" and result.target_receipt["comparison"]["blocked"]
    assert result.target_receipt["comparison"]["actions"][0]["reason"] == "rrset_answers_differ"
    assert DomainDNSReservation.objects.filter(change=result).exists()


def test_changed_configuration_or_expired_approval_prevents_target_reads(approved):
    if approved[0].target_provider == "cpanel":
        approved[2].api_token = "ROTATED"
    else:
        approved[2].token = "ROTATED"
        approved[2].headers["Authorization"] = "Bearer ROTATED"
    assert run(approved).status == "review"
    read_method(approved).assert_not_called()


def test_expired_source_plan_cannot_be_used_to_inspect_and_approve_target(approved):
    with patch("apps.domains.dns_target_inventory.timezone.now", return_value=timezone.now() + timedelta(hours=1)):
        assert run(approved).status == "review"
    read_method(approved).assert_not_called()


def test_live_lease_suppresses_duplicate_read_and_old_token_cannot_publish(approved):
    token = uuid.uuid4()
    DomainDNSChange.objects.filter(pk=approved[0].pk).update(status="target_inventory", target_token=token,
        target_lease_until=timezone.now() + timedelta(minutes=5))
    assert run(approved).target_token == token
    read_method(approved).assert_not_called()
    DomainDNSChange.objects.filter(pk=approved[0].pk).update(target_lease_until=timezone.now() - timedelta(seconds=1))
    original = read_method(approved).side_effect
    def replaced(*args, **kwargs):
        result = original(*args, **kwargs)
        DomainDNSChange.objects.filter(pk=approved[0].pk).update(target_token=token)
        return result
    read_method(approved).side_effect = replaced
    result = run(approved)
    assert result.status == "target_inventory" and not result.target_receipt and result.target_token == token


def test_cancellation_during_target_read_fences_late_receipt(approved):
    original = read_method(approved).side_effect
    def cancelled(*args, **kwargs):
        result = original(*args, **kwargs)
        cancel(approved[0])
        return result
    read_method(approved).side_effect = cancelled
    result = run(approved)
    assert result.status == "cancelled" and result.target_token is None and result.target_lease_until is None
    assert not result.target_receipt and not DomainDNSReservation.objects.filter(change=result).exists()


@pytest.mark.parametrize("action", ["domain.dns.target_started", "domain.dns.target_observed"])
def test_audit_failure_prevents_reads_or_retains_claim(approved, action):
    create = AuditLog.objects.create
    def fail(*args, **kwargs):
        if kwargs.get("action") == action:
            raise RuntimeError("audit unavailable")
        return create(*args, **kwargs)
    with patch("apps.domains.dns_target_inventory.AuditLog.objects.create", side_effect=fail):
        with pytest.raises(RuntimeError):
            run(approved)
    approved[0].refresh_from_db()
    assert not approved[0].target_receipt and DomainDNSReservation.objects.filter(change=approved[0]).exists()
    if action.endswith("started"):
        read_method(approved).assert_not_called()
        assert approved[0].status == "plan_approved" and approved[0].target_token is None
    else:
        assert approved[0].status == "target_inventory" and approved[0].target_token


def test_target_receipt_is_immutable_and_digest_tampering_is_rejected(approved):
    result = run(approved)
    result.target_receipt = {"changed": True}
    with pytest.raises(ValidationError, match="Retain"):
        result.save()
    DomainDNSChange.objects.filter(pk=result.pk).update(target_receipt={"tampered": True})
    with pytest.raises(ValidationError, match="evidence"):
        run(approved)


def test_target_reads_refuse_an_enclosing_transaction(approved):
    with transaction.atomic():
        with pytest.raises(ValidationError, match="outside"):
            run(approved)
    read_method(approved).assert_not_called()


def test_whm_error_is_not_target_absence_and_cloudflare_absence_requires_two_searches(approved):
    if approved[0].target_provider == "cpanel":
        read_method(approved).side_effect = WHMClientError("PRIVATE: missing or unauthorized")
        result = run(approved)
        assert result.status == "review" and not result.target_receipt and "PRIVATE" not in result.target_message
    else:
        read_method(approved).side_effect = lambda *args, **kwargs: zone_page([])
        result = run(approved)
        assert result.status == "target_review" and result.target_receipt["target"]["exists"] is False
        assert read_method(approved).call_count == 2
        approved[2].get_zone.assert_not_called()
        approved[2].list_dns_records.assert_not_called()
    assert DomainDNSReservation.objects.filter(change=approved[0]).exists()


def test_revoked_actor_during_target_read_cannot_publish_receipt(approved, django_user_model):
    original = read_method(approved).side_effect
    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        django_user_model.objects.filter(pk=approved[0].requested_by_id).update(is_active=False)
        return result
    read_method(approved).side_effect = revoked
    result = run(approved)
    assert result.status == "review" and not result.target_receipt
    assert DomainDNSReservation.objects.filter(change=approved[0]).exists()


def test_target_configuration_changed_during_read_is_not_recorded(approved):
    original = read_method(approved).side_effect
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if approved[0].target_provider == "cpanel":
            approved[2].api_token = "ROTATED"
        else:
            approved[2].token = "ROTATED"
            approved[2].headers["Authorization"] = "Bearer ROTATED"
        return result
    read_method(approved).side_effect = changed
    result = run(approved)
    assert result.status == "review" and not result.target_receipt
    assert DomainDNSReservation.objects.filter(change=approved[0]).exists()
