from datetime import timedelta
from unittest.mock import Mock, patch
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.cloudflare_integration.services import CloudflareError
from apps.domains.dns_zone_execution import prepare_cloudflare_zone, inspect_cloudflare_zone_submission
from apps.domains.models import DomainDNSChange, DomainDNSExecutionStep, DomainDNSReservation
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_inventory_stage import admitted
from tests.test_dns_target_inventory import build_approved, run, cancel
from tests.test_dns_target_review import approve
from tests.test_dns_preflight import configure_unsigned_clients
from tests.test_cloudflare_zones import zone, page as zone_page
from tests.test_cloudflare_inventory import page as record_page
from tests.test_cloudflare_inventory import row
from tests.test_cloudflare_scope import provider

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["adopt", "create"])
def prepared_input(admitted, request, monkeypatch):
    return build_prepared_input(admitted, request.param, monkeypatch)


def build_prepared_input(admitted, mode, monkeypatch):
    approved = build_approved(admitted, "cloudflare")
    change, source, target, registrar, domain = approved
    state = {"exists": mode == "adopt"}
    def zones(*args, **kwargs):
        assert not connection.in_atomic_block
        return zone_page([zone(name=domain.name)] if state["exists"] else [])
    target.list_zones.side_effect = zones
    if mode == "create":
        target.list_dns_records.side_effect = lambda *args, **kwargs: record_page([])
    change = run(approved)
    values = (change, source, target, registrar, domain)
    approve(values)
    configure_unsigned_clients(values, monkeypatch)
    def created(*args, **kwargs):
        assert not connection.in_atomic_block
        saved = DomainDNSChange.objects.get(pk=change.pk)
        step = DomainDNSExecutionStep.objects.get(change=saved)
        assert saved.zone_submitted_at is not None and step.submitted_at == saved.zone_submitted_at
        assert step.preflights and step.status == "submitting"
        state["exists"] = True
        return {"success": True, "result": zone(name=domain.name)}
    target.create_managed_zone = Mock(side_effect=created)
    return values, state


def execute(prepared_input):
    (change, source, target, registrar, domain), _ = prepared_input
    return prepare_cloudflare_zone(change.pk, source_client=source, target_client=target, registrar_client=registrar)


def test_zone_preparation_is_verified_durable_and_replay_never_restarts(prepared_input):
    values, state = prepared_input
    result = execute(prepared_input)
    assert result.status == "complete" and result.completed_at and result.receipt_digest and result.provider_id == "b" * 32
    assert result.receipt["delegation_updated"] is False
    assert result.request["effect"] == "prepare_zone_only"
    count = values[2].list_zones.call_count
    assert execute(prepared_input).receipt_digest == result.receipt_digest
    assert values[2].list_zones.call_count == count
    assert values[2].create_managed_zone.call_count == (1 if result.request["mode"] == "create" else 0)
    values[0].refresh_from_db()
    values[4].refresh_from_db()
    assert values[0].status == "zone_ready" and values[0].completed_at is None
    assert values[4].dns_provider == "cpanel" and values[4].cloudflare_zone_id == ""
    assert DomainDNSReservation.objects.filter(change=values[0]).exists()


def test_audit_failure_before_submission_prevents_every_post(prepared_input):
    create = AuditLog.objects.create
    def fail(*args, **kwargs):
        if kwargs.get("action") == "domain.dns.zone_authorized":
            raise RuntimeError("audit unavailable")
        return create(*args, **kwargs)
    with patch("apps.domains.dns_zone_execution.AuditLog.objects.create", side_effect=fail):
        with pytest.raises(RuntimeError):
            execute(prepared_input)
    step = DomainDNSExecutionStep.objects.get(change=prepared_input[0][0])
    assert step.submitted_at is None and step.preflights == [] and step.token
    prepared_input[0][2].create_managed_zone.assert_not_called()


def test_cancel_during_preflight_fences_worker_and_releases_only_local_work(prepared_input):
    from apps.domains.dns_zone_execution import observe_execution_inputs
    def stop(*args, **kwargs):
        proof = observe_execution_inputs(*args, **kwargs)
        cancel(prepared_input[0][0])
        return proof
    with patch("apps.domains.dns_zone_execution.observe_execution_inputs", side_effect=stop):
        result = execute(prepared_input)
    assert result.status == "cancelled" and result.submitted_at is None and result.token is None
    assert not DomainDNSReservation.objects.filter(change=prepared_input[0][0]).exists()
    prepared_input[0][2].create_managed_zone.assert_not_called()


def test_live_prepare_lease_suppresses_duplicate_preflight(prepared_input):
    from apps.domains.dns_zone_execution import observe_execution_inputs
    def duplicate(*args, **kwargs):
        second = execute(prepared_input)
        assert second.status == "preparing" and second.submitted_at is None
        return observe_execution_inputs(*args, **kwargs)
    with patch("apps.domains.dns_zone_execution.observe_execution_inputs", side_effect=duplicate) as proof:
        assert execute(prepared_input).status == "complete"
    assert proof.call_count == 1


@pytest.mark.parametrize("prepared_input", ["create"], indirect=True)
def test_submitted_timeout_is_held_and_inspection_never_retries_or_adopts(prepared_input):
    values, state = prepared_input
    values[2].create_managed_zone.side_effect = CloudflareError("PRIVATE timeout", uncertain=True)
    result = execute(prepared_input)
    assert result.status == "review" and result.submitted_at is not None and "PRIVATE" not in result.message
    assert execute(prepared_input).pk == result.pk and values[2].create_managed_zone.call_count == 1
    state["exists"] = True
    observed = inspect_cloudflare_zone_submission(actor=values[0].requested_by, change_id=values[0].pk, target_client=values[2])
    assert observed.observations[-1]["zone"]["zone_id"] == "b" * 32
    assert observed.observations[-1]["association_verified"] is False and not observed.provider_id
    assert values[2].create_managed_zone.call_count == 1
    assert DomainDNSReservation.objects.filter(change=values[0]).exists()


@pytest.mark.parametrize("prepared_input", ["create"], indirect=True)
def test_worker_crash_after_marker_never_causes_second_post(prepared_input):
    values, _ = prepared_input
    values[2].create_managed_zone.side_effect = SystemExit("simulated crash")
    with pytest.raises(SystemExit):
        execute(prepared_input)
    step = DomainDNSExecutionStep.objects.get(change=values[0])
    DomainDNSExecutionStep.objects.filter(pk=step.pk).update(lease_until=timezone.now() - timedelta(minutes=1))
    assert execute(prepared_input).submitted_at == step.submitted_at
    assert values[2].create_managed_zone.call_count == 1


@pytest.mark.parametrize("prepared_input", ["create"], indirect=True)
def test_acknowledged_zone_survives_revoked_actor_before_readback(prepared_input, django_user_model):
    values, _ = prepared_input
    original = values[2].create_managed_zone.side_effect
    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        django_user_model.objects.filter(pk=values[0].requested_by_id).update(is_active=False)
        return result
    values[2].create_managed_zone.side_effect = revoked
    result = execute(prepared_input)
    values[0].refresh_from_db()
    assert result.status == "review" and result.provider_id == values[0].zone_id == "b" * 32
    assert result.submitted_at is not None and not result.receipt_digest


def test_execution_history_and_submission_markers_cannot_be_rewritten(prepared_input):
    step = execute(prepared_input)
    step.request = {"other": True}
    with pytest.raises(ValidationError, match="original"):
        step.save()
    step.refresh_from_db()
    step.preflights = []
    with pytest.raises(ValidationError, match="observation"):
        step.save()


def test_execution_rejects_enclosing_transaction(prepared_input):
    with transaction.atomic():
        with pytest.raises(ValidationError, match="outside"):
            execute(prepared_input)
    prepared_input[0][2].create_managed_zone.assert_not_called()


def test_managed_zone_transport_uses_only_current_scoped_full_zone_contract():
    client = provider()
    response = Mock(status_code=200)
    response.json.return_value = {"success": True, "result": zone()}
    with patch("apps.cloudflare_integration.services.requests.request", return_value=response) as http:
        client.create_managed_zone("example.com", account_id="a" * 32)
        assert http.call_args.args == ("POST", client.BASE_URL + "/zones")
        assert http.call_args.kwargs["json"] == {"name": "example.com", "account": {"id": "a" * 32}, "type": "full"}
        assert http.call_args.kwargs["allow_redirects"] is False and http.call_args.kwargs["timeout"] == (10, 30)
        with pytest.raises(CloudflareError):
            client.create_managed_zone("example.com", account_id="bad")
        assert http.call_count == 1


@pytest.mark.parametrize("prepared_input", ["create"], indirect=True)
def test_unexpected_records_after_creation_require_review(prepared_input):
    values, _ = prepared_input
    values[2].list_dns_records.side_effect = lambda *args, **kwargs: record_page([row(name=values[4].name)])
    result = execute(prepared_input)
    values[0].refresh_from_db()
    assert result.status == "complete" and result.receipt["record_review_required"] is True
    assert values[0].status == "zone_review" and values[0].completed_at is None


@pytest.mark.parametrize("mutation", ["scope", "actor"])
def test_changed_authority_before_marker_prevents_every_post(prepared_input, mutation, django_user_model):
    from apps.domains.dns_zone_execution import observe_execution_inputs
    values, _ = prepared_input
    def changed(*args, **kwargs):
        proof = observe_execution_inputs(*args, **kwargs)
        if mutation == "scope":
            values[2].token = "ROTATED"
            values[2].headers["Authorization"] = "Bearer ROTATED"
        else:
            django_user_model.objects.filter(pk=values[0].requested_by_id).update(is_active=False)
        return proof
    with patch("apps.domains.dns_zone_execution.observe_execution_inputs", side_effect=changed):
        result = execute(prepared_input)
    assert result.status == "review" and result.submitted_at is None
    values[2].create_managed_zone.assert_not_called()


@pytest.mark.parametrize("prepared_input", ["create"], indirect=True)
def test_known_acknowledgement_survives_expired_lease(prepared_input):
    values, _ = prepared_input
    original = values[2].create_managed_zone.side_effect
    def expired(*args, **kwargs):
        result = original(*args, **kwargs)
        DomainDNSExecutionStep.objects.filter(change=values[0]).update(lease_until=timezone.now() - timedelta(seconds=1))
        return result
    values[2].create_managed_zone.side_effect = expired
    result = execute(prepared_input)
    assert result.status == "review" and result.provider_id == "b" * 32 and result.submitted_at is not None


@pytest.mark.parametrize("prepared_input", ["create"], indirect=True)
def test_absent_zone_after_uncertain_post_never_authorizes_retry(prepared_input):
    values, _ = prepared_input
    values[2].create_managed_zone.side_effect = CloudflareError("timeout", uncertain=True)
    execute(prepared_input)
    result = inspect_cloudflare_zone_submission(actor=values[0].requested_by, change_id=values[0].pk, target_client=values[2])
    assert result.observations[-1]["zone"] is None and result.observations[-1]["retry_authorized"] is False
    assert execute(prepared_input).submitted_at is not None and values[2].create_managed_zone.call_count == 1


def test_replaced_preflight_token_prevents_stale_worker_submission(prepared_input):
    from apps.domains.dns_zone_execution import observe_execution_inputs
    replacement = uuid.uuid4()
    def replaced(*args, **kwargs):
        proof = observe_execution_inputs(*args, **kwargs)
        DomainDNSExecutionStep.objects.filter(change=prepared_input[0][0]).update(token=replacement)
        return proof
    with patch("apps.domains.dns_zone_execution.observe_execution_inputs", side_effect=replaced):
        result = execute(prepared_input)
    assert result.token == replacement and result.submitted_at is None
    prepared_input[0][2].create_managed_zone.assert_not_called()


@pytest.mark.parametrize("event", ["domain.dns.zone_started", "domain.dns.zone_prepared"])
def test_start_and_completion_audits_do_not_lose_known_outcomes(prepared_input, event):
    create = AuditLog.objects.create
    count = prepared_input[0][2].list_zones.call_count
    def fail(*args, **kwargs):
        if kwargs.get("action") == event:
            raise RuntimeError("audit unavailable")
        return create(*args, **kwargs)
    with patch("apps.domains.dns_zone_execution.AuditLog.objects.create", side_effect=fail):
        with pytest.raises(RuntimeError):
            execute(prepared_input)
    if event.endswith("started"):
        assert not DomainDNSExecutionStep.objects.filter(change=prepared_input[0][0]).exists()
        assert prepared_input[0][2].list_zones.call_count == count
        prepared_input[0][2].create_managed_zone.assert_not_called()
    else:
        step = DomainDNSExecutionStep.objects.get(change=prepared_input[0][0])
        assert not step.receipt_digest and step.token
        if step.request["mode"] == "create":
            assert step.provider_id == "b" * 32 and step.submitted_at
            execute(prepared_input)
            assert prepared_input[0][2].create_managed_zone.call_count == 1


@pytest.mark.parametrize("inconsistent", [{"status": "unknown"}, {"provider_id": "c" * 32}])
def test_unknown_or_acknowledged_state_without_parent_marker_is_not_auto_retried(prepared_input, inconsistent):
    with patch("apps.domains.dns_zone_execution.observe_execution_inputs", side_effect=RuntimeError("preflight worker stopped")):
        with pytest.raises(RuntimeError):
            execute(prepared_input)
    DomainDNSExecutionStep.objects.filter(change=prepared_input[0][0]).update(
        lease_until=timezone.now() - timedelta(seconds=1), **inconsistent)
    with pytest.raises(ValidationError, match="reconciliation"):
        execute(prepared_input)
    prepared_input[0][2].create_managed_zone.assert_not_called()


def test_completed_receipt_rejects_boolean_to_number_changes(prepared_input):
    step = execute(prepared_input)
    step.refresh_from_db()
    step.receipt["zone_preparation_complete"] = 1
    with pytest.raises(ValidationError, match="completed"):
        step.save(update_fields=["receipt"])


def test_completed_receipt_can_save_message_without_rewriting_tuple_array_evidence(prepared_input):
    step = execute(prepared_input)
    step.message = "Verified preparation retained."
    step.save(update_fields=["message"])
    step.refresh_from_db()
    assert step.message == "Verified preparation retained." and step.receipt["zone_preparation_complete"] is True
