from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.dns_record_execution import prepare_target_records, inspect_target_records, reconcile_target_records
from apps.domains.models import DomainDNSChange, DomainDNSExecutionStep, DomainDNSReservation
from apps.provisioning.whm_client import WHMClientError
from tests.test_dns_apply_review import create, approve
from tests.test_whm_zone_execution import whm_ready, execute
from tests.test_dns_inventory_stage import admitted
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_whm_dns_inventory import rr, b64
from tests.test_dns_target_inventory import build_approved, run as collect_target
from tests.test_dns_target_review import approve as approve_target
from tests.test_dns_preflight import configure_unsigned_clients
from tests.test_cloudflare_zones import zone, page as zone_page
from tests.test_cloudflare_inventory import page as record_page
from apps.domains.dns_zone_execution import prepare_cloudflare_zone
from apps.cloudflare_integration.services import CloudflareError

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def isolated_parent_observations(monkeypatch):
    from apps.domains import dns_preflight
    monkeypatch.setattr("apps.domains.dns_record_execution.read_parent_ds", lambda name: dns_preflight.read_parent_ds(name))


@pytest.fixture
def writable(whm_ready):
    change, source, target, registrar, domain = whm_ready[0]
    state = {"applied": False, "serial": None}
    original = target.parse_dns_zone.side_effect
    def parsed(name):
        value = original(name)
        value["data"]["payload"] = [row for row in value["data"]["payload"] if row.get("record_type") != "MX"]
        soa = next(row for row in value["data"]["payload"] if row.get("record_type") == "SOA")
        if state["applied"]:
            soa["data_b64"][2] = b64(str(state["serial"]))
            for index, row in enumerate(state["added"]):
                entry = rr(20 + index, row["record_type"], row["data"], row["dname"])
                entry["ttl"] = row["ttl"]
                value["data"]["payload"].append(entry)
        return value
    target.parse_dns_zone.side_effect = parsed
    execute(whm_ready)
    plan = approve(whm_ready, create(whm_ready))
    def applied(payload):
        import json
        assert not connection.in_atomic_block
        step = DomainDNSExecutionStep.objects.get(change=change, stage="records")
        assert step.submitted_at and step.preflights and step.status == "submitting"
        assert DomainDNSChange.objects.get(pk=change.pk).status == "record_writing"
        assert payload == step.request["payload"] and payload["add"]
        state.update(applied=True, serial=payload["serial"] + 1, added=[json.loads(row) for row in payload["add"]])
        return {"new_serial": state["serial"]}
    target.apply_dns_batch = Mock(side_effect=applied)
    return whm_ready, plan, state


def run(values):
    change, source, target, registrar, domain = values
    return prepare_target_records(change.pk, source_client=source, target_client=target, registrar_client=registrar)


@pytest.fixture
def cloudflare_writable(admitted, monkeypatch, request):
    values = build_approved(admitted, "cloudflare")
    change, source, target, registrar, domain = values
    state = {"exists": False, "rows": []}
    servers = ["ns1.example.test", "cf2.example.test"] if getattr(request, "param", None) == "overlap" else ["cf1.example.test", "cf2.example.test"]
    remote_zone = zone(name=domain.name, name_servers=servers)
    target.list_zones = Mock(side_effect=lambda *args, **kwargs: zone_page([remote_zone] if state["exists"] else []))
    target.get_zone = Mock(side_effect=lambda *args: {"success": True, "result": deepcopy(remote_zone)})
    target.list_dns_records = Mock(side_effect=lambda *args, **kwargs: record_page(deepcopy(state["rows"])))
    change = collect_target(values)
    values = change, source, target, registrar, domain
    approve_target(values)
    configure_unsigned_clients(values, monkeypatch)
    def created(*args, **kwargs):
        state["exists"] = True
        return {"success": True, "result": deepcopy(remote_zone)}
    target.create_managed_zone = Mock(side_effect=created)
    prepare_cloudflare_zone(change.pk, source_client=source, target_client=target, registrar_client=registrar)
    ready = values, state
    plan = approve(ready, create(ready))
    def applied(zone_id, payload):
        assert not connection.in_atomic_block and zone_id == "b" * 32
        step = DomainDNSExecutionStep.objects.get(change=change, stage="records")
        assert step.submitted_at and step.request["payload"] == payload
        assert payload["posts"] and not payload["deletes"]
        state["rows"] = [dict(id=f"{index + 50:032x}", **row) for index, row in enumerate(payload["posts"])]
        return {"success": True, "result": {"posts": deepcopy(state["rows"]), "deletes": []}}
    target.apply_dns_batch = Mock(side_effect=applied)
    return ready, plan, state


def test_cloudflare_records_are_created_once_and_verified_without_delegation(cloudflare_writable):
    ready, plan, state = cloudflare_writable
    result = run(ready[0])
    assert result.status == "complete" and result.receipt["records_preparation_complete"] is True
    assert result.submitted_at and result.provider_id == "b" * 32
    assert run(ready[0]).receipt_digest == result.receipt_digest and ready[0][2].apply_dns_batch.call_count == 1
    assert state["rows"][0]["type"] == "MX" and state["rows"][0]["priority"] == 10
    ready[0][4].refresh_from_db()
    assert ready[0][4].dns_provider == "cpanel" and ready[0][4].cloudflare_zone_id == ""


def test_cloudflare_timeout_keeps_exact_batch_without_repeating_it(cloudflare_writable):
    ready, _, _ = cloudflare_writable
    ready[0][2].apply_dns_batch.side_effect = CloudflareError("PRIVATE", uncertain=True)
    result = run(ready[0])
    assert result.status == "review" and result.submitted_at and not result.receipt_digest
    assert run(ready[0]).pk == result.pk and ready[0][2].apply_dns_batch.call_count == 1


@pytest.mark.parametrize("cloudflare_writable", ["overlap"], indirect=True)
def test_target_sharing_a_live_authoritative_server_requires_separate_review(cloudflare_writable):
    ready, _, _ = cloudflare_writable
    result = run(ready[0])
    assert result.status == "review" and result.submitted_at is None
    ready[0][2].apply_dns_batch.assert_not_called()


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_whm_batch_is_journaled_before_http_and_complete_readback_does_not_activate_dns(writable):
    ready, plan, state = writable
    result = run(ready[0])
    assert result.status == "complete" and result.receipt["records_preparation_complete"] is True
    assert result.receipt["delegation_updated"] is False and result.receipt["public_activation_verified"] is False
    assert result.receipt["target"]["zone"]["serial"] == state["serial"]
    assert run(ready[0]).receipt_digest == result.receipt_digest and ready[0][2].apply_dns_batch.call_count == 1
    change = DomainDNSChange.objects.get(pk=plan.change_id)
    assert change.status == "records_ready" and change.completed_at is None
    ready[0][4].refresh_from_db()
    assert ready[0][4].dns_provider == "cloudflare" and DomainDNSReservation.objects.filter(change=change).exists()


def test_matching_records_complete_without_a_provider_write(whm_ready):
    execute(whm_ready)
    approve(whm_ready, create(whm_ready))
    whm_ready[0][2].apply_dns_batch = Mock(side_effect=AssertionError("no write"))
    result = run(whm_ready[0])
    assert result.status == "complete" and result.submitted_at is None


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_timeout_retains_submission_marker_and_never_repeats_batch(writable):
    ready, _, _ = writable
    ready[0][2].apply_dns_batch.side_effect = WHMClientError("PRIVATE timeout")
    result = run(ready[0])
    assert result.status == "review" and result.submitted_at and "PRIVATE" not in result.message
    assert run(ready[0]).submitted_at == result.submitted_at and ready[0][2].apply_dns_batch.call_count == 1


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_acknowledged_but_wrong_readback_is_held_without_retry(writable):
    ready, _, _ = writable
    ready[0][2].apply_dns_batch.side_effect = None
    ready[0][2].apply_dns_batch.return_value = {"new_serial": 123}
    result = run(ready[0])
    assert result.status == "review" and result.submitted_at and result.provider_id
    assert result.observations and not result.receipt_digest
    assert run(ready[0]).pk == result.pk and ready[0][2].apply_dns_batch.call_count == 1


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_audit_failure_before_submission_rolls_back_marker_and_prevents_provider_call(writable):
    ready, _, _ = writable
    original = AuditLog.objects.create
    def failed(*args, **kwargs):
        if kwargs.get("action") == "domain.dns.records_authorized":
            raise RuntimeError("audit unavailable")
        return original(*args, **kwargs)
    with patch("apps.domains.dns_record_execution.AuditLog.objects.create", side_effect=failed):
        with pytest.raises(RuntimeError):
            run(ready[0])
    ready[0][2].apply_dns_batch.assert_not_called()
    assert DomainDNSExecutionStep.objects.get(change=ready[0][0], stage="records").submitted_at is None


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_changed_target_or_security_is_not_treated_as_empty_zone(writable):
    ready, _, _ = writable
    ready[0][2].get_dnssec_status.side_effect = None
    ready[0][2].get_dnssec_status.return_value = {"PRIVATE": "unrecognized"}
    result = run(ready[0])
    assert result.status == "review" and result.submitted_at is None
    ready[0][2].apply_dns_batch.assert_not_called()


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_revoked_actor_after_http_preserves_acknowledgement_without_claiming_completion(writable):
    ready, _, _ = writable
    original = ready[0][2].apply_dns_batch.side_effect
    def revoked(payload):
        value = original(payload)
        user = ready[0][0].requested_by
        type(user).objects.filter(pk=user.pk).update(is_active=False)
        return value
    ready[0][2].apply_dns_batch.side_effect = revoked
    result = run(ready[0])
    assert result.status == "review" and result.provider_id and result.observations and not result.receipt_digest


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_preparation_cannot_run_in_an_open_transaction(writable):
    from django.db import transaction
    with transaction.atomic():
        with pytest.raises(ValidationError, match="outside"):
            run(writable[0][0])


def inspect(ready):
    change, source, target, registrar, domain = ready[0]
    return inspect_target_records(actor=change.requested_by, change_id=change.pk,
        source_client=source, target_client=target, registrar_client=registrar)


def reconcile(ready, observed, **kwargs):
    change, source, target, registrar, domain = ready[0]
    options = dict(actor=change.requested_by, change_id=change.pk,
        observation_digest=observed.observations[-1]["digest"], confirmed=True,
        source_client=source, target_client=target, registrar_client=registrar)
    options.update(kwargs)
    return reconcile_target_records(**options)


def uncertain_after_apply(ready, error):
    target = ready[0][2]
    original = target.apply_dns_batch.side_effect
    def uncertain(*args):
        original(*args)
        raise error
    target.apply_dns_batch.side_effect = uncertain
    failed = run(ready[0])
    assert failed.status == "review" and failed.submitted_at and not failed.provider_id
    return failed


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_uncertain_whm_batch_can_be_explicitly_reconciled_without_repeating_it(writable):
    ready, plan, _ = writable
    uncertain_after_apply(ready, WHMClientError("uncertain"))
    observed = inspect(ready)
    assert observed.observations[-1]["retry_authorized"] is False
    with pytest.raises(ValidationError):
        reconcile(ready, observed, confirmed=1)
    with pytest.raises(ValidationError):
        reconcile(ready, observed, observation_digest="unrelated")
    result = reconcile(ready, observed)
    assert result.status == "complete" and result.receipt["records_preparation_complete"] is True
    assert result.receipt["provider_action_complete"] is False and result.receipt["original_submission_unproven"] is True
    assert result.submitted_at and not result.provider_id
    assert reconcile(ready, observed).receipt_digest == result.receipt_digest
    assert run(ready[0]).receipt_digest == result.receipt_digest and ready[0][2].apply_dns_batch.call_count == 1


def test_uncertain_cloudflare_batch_can_be_observed_and_explicitly_accepted(cloudflare_writable):
    ready, _, _ = cloudflare_writable
    uncertain_after_apply(ready, CloudflareError("uncertain", uncertain=True))
    result = reconcile(ready, inspect(ready))
    assert result.status == "complete" and result.receipt["original_submission_unproven"] is True
    assert ready[0][2].apply_dns_batch.call_count == 1


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_inspecting_missing_effects_never_authorizes_repeating_the_batch(writable):
    ready, _, _ = writable
    ready[0][2].apply_dns_batch.side_effect = WHMClientError("uncertain")
    run(ready[0])
    observed = inspect(ready)
    with pytest.raises(ValidationError, match="complete target answers"):
        reconcile(ready, observed)
    assert run(ready[0]).submitted_at and ready[0][2].apply_dns_batch.call_count == 1


@pytest.mark.parametrize("whm_ready", ["create"], indirect=True)
def test_record_reconciliation_requires_the_target_to_still_match_its_inspection(writable):
    ready, _, state = writable
    uncertain_after_apply(ready, WHMClientError("uncertain"))
    observed = inspect(ready)
    state["serial"] += 1
    result = reconcile(ready, observed)
    assert result.status == "review" and not result.receipt_digest and ready[0][2].apply_dns_batch.call_count == 1
