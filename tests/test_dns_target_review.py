from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.contact_bindings import _fingerprint
from apps.domains.dns_target_review import approve_target_plan
from apps.domains.models import DomainDNSChange, DomainDNSReservation
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_inventory_stage import admitted
from tests.test_dns_target_inventory import approved, run, cancel, read_method
from tests.test_cloudflare_inventory import row, page as record_page
from tests.test_whm_dns_inventory import rr

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def reviewed(approved):
    change = run(approved)
    return (change, *approved[1:])


def approve(reviewed, **kwargs):
    change, source, target, registrar, domain = reviewed
    values = dict(actor=change.requested_by, change_id=change.pk, digest=change.target_digest, confirmed=True,
        acknowledgements=change.target_receipt["comparison"]["required_acknowledgements"],
        source_client=source, target_client=target, registrar_client=registrar)
    values.update(kwargs)
    return approve_target_plan(**values)


def test_exact_target_approval_is_durable_offline_and_replay_preserves_attribution(reviewed):
    reads = read_method(reviewed).call_count
    with patch("requests.request", side_effect=AssertionError("No provider writes or reads")):
        result = approve(reviewed)
        repeat = approve(reviewed)
    assert result.status == "target_approved" and result.target_approved_by_id == reviewed[0].requested_by_id
    assert repeat.target_approved_at == result.target_approved_at and repeat.target_approved_digest == reviewed[0].target_digest
    assert read_method(reviewed).call_count == reads
    assert result.zone_submitted_at is None and not result.zone_id
    assert DomainDNSReservation.objects.filter(change=result).exists()
    assert AuditLog.objects.filter(action="domain.dns.target_approved", object_id=str(result.pk)).count() == 1


def test_expired_source_plan_or_target_observation_prevents_new_approval(reviewed):
    with patch("apps.domains.dns_target_review.timezone.now", return_value=timezone.now() + timedelta(hours=1)):
        with pytest.raises(ValidationError, match="expired"):
            approve(reviewed)
    assert DomainDNSReservation.objects.filter(change=reviewed[0]).exists()


def test_approval_replay_after_expiry_and_cancellation_is_history_only(reviewed):
    original = approve(reviewed)
    cancel(reviewed[0])
    with patch("apps.domains.dns_target_review.timezone.now", return_value=timezone.now() + timedelta(hours=1)):
        replay = approve(reviewed)
    assert replay.status == "cancelled" and replay.target_approved_at == original.target_approved_at
    assert not DomainDNSReservation.objects.filter(change=replay).exists()


@pytest.mark.parametrize("values", [
    {"confirmed": "true"}, {"confirmed": 1}, {"acknowledgements": "retain_target_only_records"},
    {"acknowledgements": [True]}, {"acknowledgements": ["unknown"]},
    {"acknowledgements": ["unknown", "unknown"]}, {"digest": "wrong"},
])
def test_target_consent_is_typed_exact_and_cannot_be_guessed(reviewed, values):
    with pytest.raises(ValidationError):
        approve(reviewed, **values)
    reviewed[0].refresh_from_db()
    assert reviewed[0].target_approved_at is None


def test_foreign_staff_and_revoked_actor_cannot_approve(reviewed, django_user_model):
    for extra in ({}, {"is_staff": True}):
        actor = django_user_model.objects.create_user(email=f"target-review-{len(extra)}@example.test", password="testpass", **extra)
        with pytest.raises(PermissionDenied):
            approve(reviewed, actor=actor)
    django_user_model.objects.filter(pk=reviewed[0].requested_by_id).update(is_active=False)
    with pytest.raises(PermissionDenied):
        approve(reviewed)


def test_owner_can_approve_for_customer_without_changing_history_on_replay(reviewed, django_user_model):
    actor = django_user_model.objects.create_user(email="target-review-owner@example.test", password="testpass", is_staff=True, is_superuser=True)
    result = approve(reviewed, actor=actor)
    assert result.target_approved_by_id == actor.pk and approve(reviewed).target_approved_by_id == actor.pk


def test_target_approval_audit_failure_rolls_back_consent_and_status(reviewed):
    with patch("apps.domains.dns_target_review.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            approve(reviewed)
    reviewed[0].refresh_from_db()
    assert reviewed[0].status == "target_review" and reviewed[0].target_approved_at is None
    assert DomainDNSReservation.objects.filter(change=reviewed[0]).exists()


def test_approval_history_cannot_be_reset_or_reassigned(reviewed):
    result = approve(reviewed)
    result.target_approved_digest = "x" * 64
    with pytest.raises(ValidationError, match="original"):
        result.save()


def test_rehashed_target_comparison_cannot_bypass_source_and_target_binding(reviewed):
    receipt = deepcopy(reviewed[0].target_receipt)
    receipt["comparison"]["actions"] = []
    DomainDNSChange.objects.filter(pk=reviewed[0].pk).update(target_receipt=receipt, target_digest=_fingerprint(receipt))
    reviewed[0].refresh_from_db()
    with pytest.raises(ValidationError, match="comparison"):
        approve(reviewed)


def test_source_drift_after_target_read_prevents_approval(reviewed):
    reviewed[4].name = "changed.example.com"
    reviewed[4].save(update_fields=["name"])
    with pytest.raises(ValidationError):
        approve(reviewed)


def test_backend_drift_after_target_read_prevents_approval(reviewed):
    if reviewed[0].target_provider == "cpanel":
        reviewed[2].api_token = "ROTATED"
    else:
        reviewed[2].token = "ROTATED"
        reviewed[2].headers["Authorization"] = "Bearer ROTATED"
    with pytest.raises(ValidationError):
        approve(reviewed)
    reviewed[0].refresh_from_db()
    assert reviewed[0].target_approved_at is None


def test_old_target_snapshot_alone_is_not_approvable(reviewed):
    DomainDNSChange.objects.filter(pk=reviewed[0].pk).update(target_observed_at=timezone.now() - timedelta(minutes=31))
    with pytest.raises(ValidationError, match="expired"):
        approve(reviewed)


def test_cancelled_unapproved_target_cannot_be_approved(reviewed):
    cancel(reviewed[0])
    with pytest.raises(ValidationError, match="awaiting"):
        approve(reviewed)


def test_target_only_records_need_explicit_acknowledgement(approved):
    if approved[0].target_provider == "cpanel":
        data = read_method(approved).side_effect(approved[4].name)
        data["data"]["payload"].append(rr(6, "TXT", ["Keep"], "other." + approved[4].name + "."))
        read_method(approved).side_effect = lambda *args: deepcopy(data)
    else:
        approved[2].list_dns_records.side_effect = lambda *args, **kwargs: record_page([
            row(name=approved[4].name, ttl=14400),
            row(2, name="other." + approved[4].name, type="TXT", content='"Keep"', ttl=14400)])
    change = run(approved)
    reviewed = (change, *approved[1:])
    assert change.target_receipt["comparison"]["required_acknowledgements"] == ["retain_target_only_records"]
    with pytest.raises(ValidationError, match="acknowledgements"):
        approve(reviewed, acknowledgements=[])
    assert approve(reviewed).target_acknowledgements == ["retain_target_only_records"]


def test_conflicting_target_is_never_approvable(approved):
    if approved[0].target_provider == "cpanel":
        data = read_method(approved).side_effect(approved[4].name)
        data["data"]["payload"][5]["data_b64"][0] = "MjA="
        read_method(approved).side_effect = lambda *args: deepcopy(data)
    else:
        approved[2].list_dns_records.side_effect = lambda *args, **kwargs: record_page([row(name=approved[4].name, ttl=14400, priority=20)])
    change = run(approved)
    with pytest.raises(ValidationError, match="conflict"):
        approve((change, *approved[1:]))
    change.refresh_from_db()
    assert change.target_approved_at is None and DomainDNSReservation.objects.filter(change=change).exists()
