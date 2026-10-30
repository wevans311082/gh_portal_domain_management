from copy import deepcopy
from unittest.mock import Mock, patch
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import connection

from apps.audit.models import AuditLog
from apps.domains.dns_changes import create_dns_preview, confirm_dns_change
from apps.domains.dns_inventory import collect_dns_inventory
from apps.domains.dns_plan_review import create_record_plan, approve_record_plan
from apps.domains.dns_target_inventory import collect_target_inventory
from apps.domains.dns_zone_execution import prepare_whm_zone, inspect_whm_zone_submission
from apps.domains.dns_zone_recovery import resolve_whm_zone, cancel_prepared_dns_change
from apps.domains.models import DomainDNSChange, DomainDNSExecutionStep, DomainDNSReservation
from apps.provisioning.whm_client import WHMClientError
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_inventory_stage import admitted
from tests.test_dns_switch_admission import confirm
from tests.test_dns_target_inventory import cancel
from tests.test_dns_target_review import approve
from tests.test_dns_preflight import configure_unsigned_clients
from tests.test_whm_dns_inventory import payload, b64
from tests.test_whm_dns_discovery import listed

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["adopt", "create"])
def whm_ready(admitted, request, monkeypatch):
    old, source, target, registrar, domain = admitted
    cancel(old)
    target.dns_nameservers = ("whm1.example.test", "whm2.example.test")
    target.dns_zone_ip = "192.0.2.44"
    state = {"exists": request.param == "adopt"}
    raw = payload(domain.name)
    raw["data"]["payload"] = raw["data"]["payload"][:6]
    raw["data"]["payload"][2]["data_b64"][0] = b64("whm1.example.test.")
    raw["data"]["payload"][3]["data_b64"] = [b64("whm1.example.test.")]
    raw["data"]["payload"][4]["data_b64"] = [b64("whm2.example.test.")]
    for item in raw["data"]["payload"]:
        if item["type"] == "record":
            item["ttl"] = 3600
    def zones():
        assert not connection.in_atomic_block
        return listed([domain.name] if state["exists"] else [])
    def records(name):
        assert not connection.in_atomic_block and state["exists"]
        return deepcopy(raw)
    target.list_dns_zones, target.parse_dns_zone = Mock(side_effect=zones), Mock(side_effect=records)
    change = create_dns_preview(actor=old.requested_by, domain_id=domain.pk, target_provider="cpanel", key=uuid.uuid4(), client=target, source_client=source)
    confirm(change, client=target, source_client=source)
    options = dict(source_client=source, target_client=target, registrar_client=registrar)
    collect_dns_inventory(change.pk, **options)
    plan = create_record_plan(actor=change.requested_by, change_id=change.pk, **options)
    approve_record_plan(actor=change.requested_by, plan_id=plan.pk, digest=plan.digest, confirmed=True,
        acknowledgements=plan.plan["required_acknowledgements"], **options)
    change = collect_target_inventory(change.pk, **options)
    values = change, source, target, registrar, domain
    approve(values)
    configure_unsigned_clients(values, monkeypatch)
    def created(name, *, ip):
        assert not connection.in_atomic_block and ip == "192.0.2.44" and name == domain.name
        saved = DomainDNSChange.objects.get(pk=change.pk)
        step = DomainDNSExecutionStep.objects.get(change=saved)
        assert saved.zone_submitted_at == step.submitted_at and saved.zone_submitted_at
        assert step.request["zone_creation"] == saved.zone_creation
        state["exists"] = True
        return True
    target.create_dns_zone = Mock(side_effect=created)
    return values, state


def execute(whm_ready):
    (change, source, target, registrar, domain), _ = whm_ready
    return prepare_whm_zone(change.pk, source_client=source, target_client=target, registrar_client=registrar)


def test_whm_prepare_is_durable_scoped_and_never_changes_live_source_delegation(whm_ready):
    values, _ = whm_ready
    result = execute(whm_ready)
    assert result.status == "complete" and result.provider_id == values[4].name and result.receipt["zone_preparation_complete"] is True
    assert result.identity == "cpanel" and result.receipt["delegation_updated"] is False
    assert result.receipt["target"]["zone"]["nameservers"] == ("whm1.example.test", "whm2.example.test")
    count = values[2].list_dns_zones.call_count
    assert execute(whm_ready).receipt_digest == result.receipt_digest and values[2].list_dns_zones.call_count == count
    assert values[2].create_dns_zone.call_count == (1 if result.request["mode"] == "create" else 0)
    values[0].refresh_from_db()
    values[4].refresh_from_db()
    assert values[4].dns_provider == "cloudflare" and values[4].cloudflare_zone_id == "b" * 32
    assert values[4].cached_nameservers == ["ns1.example.test", "ns2.example.test"]
    assert values[0].status == ("zone_review" if result.request["mode"] == "create" else "zone_ready")
    assert DomainDNSReservation.objects.filter(change=values[0]).exists()


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_whm_timeout_retains_marker_and_never_retries_mutating_get(whm_ready):
    values, _ = whm_ready
    values[2].create_dns_zone.side_effect = WHMClientError("PRIVATE uncertain result")
    result = execute(whm_ready)
    assert result.status == "review" and result.submitted_at and "PRIVATE" not in result.message
    assert execute(whm_ready).submitted_at == result.submitted_at and values[2].create_dns_zone.call_count == 1


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_changed_creation_address_requires_new_review_before_any_mutation(whm_ready):
    whm_ready[0][2].dns_zone_ip = "192.0.2.55"
    with pytest.raises(ValidationError, match="creation settings"):
        execute(whm_ready)
    whm_ready[0][2].create_dns_zone.assert_not_called()


def test_zone_creation_inputs_are_immutable(whm_ready):
    change = whm_ready[0][0]
    change.refresh_from_db()
    change.zone_creation = {"ip": "192.0.2.55"}
    with pytest.raises(ValidationError, match="preview"):
        change.save()


def test_whm_authorization_audit_failure_prevents_mutation(whm_ready):
    create = AuditLog.objects.create
    def failed(*args, **kwargs):
        if kwargs.get("action") == "domain.dns.zone_authorized":
            raise RuntimeError("audit unavailable")
        return create(*args, **kwargs)
    with patch("apps.domains.dns_zone_execution.AuditLog.objects.create", side_effect=failed):
        with pytest.raises(RuntimeError):
            execute(whm_ready)
    whm_ready[0][2].create_dns_zone.assert_not_called()


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_whm_unknown_create_can_be_inspected_and_explicitly_adopted_without_retry(whm_ready):
    values, state = whm_ready
    values[2].create_dns_zone.side_effect = WHMClientError("uncertain")
    failed = execute(whm_ready)
    state["exists"] = True
    observed = inspect_whm_zone_submission(actor=values[0].requested_by, change_id=values[0].pk, target_client=values[2])
    result = resolve_whm_zone(actor=values[0].requested_by, change_id=values[0].pk, target_client=values[2],
        confirmed=True, observation_digest=observed.observations[-1]["digest"], zone_id=values[4].name)
    assert result.status == "complete" and result.submitted_at == failed.submitted_at
    assert result.receipt["resolution"]["original_submission_outcome"] == "unproven"
    assert result.receipt["provider_action_complete"] is False
    assert values[2].create_dns_zone.call_count == 1 and execute(whm_ready).receipt_digest == result.receipt_digest


def test_verified_whm_preparation_can_be_cancelled_without_remote_cleanup(whm_ready):
    values, _ = whm_ready
    step = execute(whm_ready)
    count = values[2].list_dns_zones.call_count
    result = cancel_prepared_dns_change(actor=values[0].requested_by, change_id=values[0].pk, manifest_digest=values[0].manifest_digest,
        receipt_digest=step.receipt_digest, confirmed=True, keep_zone=True, reason="user_cancelled")
    assert result.status == "cancelled" and result.zone_id == values[4].name
    assert values[2].list_dns_zones.call_count == count and not DomainDNSReservation.objects.filter(change=result).exists()


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_no_whm_auto_retry_even_after_uncertain_zone_is_not_found(whm_ready):
    values, _ = whm_ready
    values[2].create_dns_zone.side_effect = WHMClientError("uncertain")
    execute(whm_ready)
    observed = inspect_whm_zone_submission(actor=values[0].requested_by, change_id=values[0].pk, target_client=values[2])
    assert observed.observations[-1]["zone"] is None and observed.observations[-1]["retry_authorized"] is False
    assert execute(whm_ready).submitted_at is not None and values[2].create_dns_zone.call_count == 1


def test_confirmation_rejects_changed_creation_settings_before_reserving(admitted):
    old, source, target, registrar, domain = admitted
    cancel(old)
    target.dns_zone_ip = "192.0.2.44"
    target.dns_nameservers = ("whm1.example.test", "whm2.example.test")
    change = create_dns_preview(actor=old.requested_by, domain_id=domain.pk, target_provider="cpanel", key=uuid.uuid4(),
        client=target, source_client=source)
    assert change.zone_creation["ip"] == "192.0.2.44" and change.zone_creation["nameservers"] == ["whm1.example.test", "whm2.example.test"]
    target.dns_zone_ip = "192.0.2.55"
    with pytest.raises(ValidationError, match="configuration changed"):
        confirm(change, client=target, source_client=source)
    assert not DomainDNSReservation.objects.filter(change=change).exists()
