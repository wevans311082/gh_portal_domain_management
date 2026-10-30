from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.audit.models import AuditLog
from apps.domains.dns_credential_recovery import inspect_cloudflare_zone_current_account
from apps.domains.dns_zone_recovery import resolve_cloudflare_zone_after_credential_change
from apps.domains.models import DomainDNSRecoveryEvidence, DomainDNSReservation
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_inventory_stage import admitted
from tests.test_dns_zone_recovery import uncertain, cancel_prepared
from tests.test_cloudflare_scope import provider
from tests.test_dns_zone_execution import execute

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(email="credential-recovery-owner@example.test", password="testpass", is_staff=True, is_superuser=True)


@pytest.fixture
def rotated(uncertain):
    old = uncertain[0][0][2]
    client = provider(token="REPLACEMENT-TOKEN")
    client.list_zones, client.get_zone, client.list_dns_records = old.list_zones, old.get_zone, old.list_dns_records
    client.create_managed_zone = Mock(side_effect=AssertionError("Recovery must never create a zone"))
    return client


def inspect(uncertain, owner, rotated, **kwargs):
    values = dict(actor=owner, change_id=uncertain[0][0][0].pk, target_client=rotated)
    values.update(kwargs)
    return inspect_cloudflare_zone_current_account(**values)


def resolve(uncertain, owner, rotated, evidence, **kwargs):
    values = dict(actor=owner, change_id=uncertain[0][0][0].pk, target_client=rotated,
        evidence_id=evidence.pk, evidence_digest=evidence.digest, zone_id="b" * 32, confirmed=True)
    values.update(kwargs)
    return resolve_cloudflare_zone_after_credential_change(**values)


def test_owner_can_reconcile_same_account_without_rebinding_old_consent(uncertain, owner, rotated):
    change = uncertain[0][0][0]
    change.refresh_from_db()
    original_scope, approved_at, manifest = change.provider_scope, change.target_approved_at, change.manifest_digest
    evidence = inspect(uncertain, owner, rotated)
    assert evidence.provider_scope != original_scope and evidence.body["scope_changed"] is True
    assert evidence.body["account_id"] == "a" * 32 and evidence.body["writes_authorized"] is False
    result = resolve(uncertain, owner, rotated, evidence)
    change.refresh_from_db()
    assert change.provider_scope == original_scope and change.target_approved_at == approved_at and change.manifest_digest == manifest
    assert result.request["provider_scope"] == original_scope and result.submitted_at
    assert result.receipt["new_execution_consent_required"] is True and change.status == "zone_review"
    recovery = result.receipt["resolution"]["credential_recovery"]
    assert recovery["approved_provider_scope"] == original_scope and recovery["observed_provider_scope"] == evidence.provider_scope
    assert recovery["writes_authorized"] is False and result.receipt["provider_action_complete"] is False
    assert "REPLACEMENT-TOKEN" not in str(evidence.body) + str(result.receipt)
    audit = AuditLog.objects.get(action="domain.dns.zone_resolution_confirmed", object_id=str(result.pk))
    assert audit.data["credential_recovery"]["evidence_id"] == str(evidence.pk)
    count = rotated.list_zones.call_count
    assert resolve(uncertain, owner, rotated, evidence).receipt_digest == result.receipt_digest
    assert rotated.list_zones.call_count == count
    rotated.create_managed_zone.assert_not_called()
    assert uncertain[0][0][2].create_managed_zone.call_count == 1
    assert DomainDNSReservation.objects.filter(change=change).exists()
    assert cancel_prepared(uncertain[0], result).status == "cancelled"


def test_customer_and_ordinary_staff_cannot_use_credential_recovery(uncertain, owner, rotated, django_user_model):
    staff = django_user_model.objects.create_user(email="credential-recovery-staff@example.test", password="testpass", is_staff=True)
    for actor in (uncertain[0][0][0].requested_by, staff):
        with pytest.raises(PermissionDenied):
            inspect(uncertain, owner, rotated, actor=actor)
    evidence = inspect(uncertain, owner, rotated)
    for actor in (uncertain[0][0][0].requested_by, staff):
        with pytest.raises(PermissionDenied):
            resolve(uncertain, owner, rotated, evidence, actor=actor)


def test_different_account_is_rejected_before_provider_read(uncertain, owner, rotated):
    rotated.account_id = "c" * 32
    count = rotated.list_zones.call_count
    with pytest.raises(ValidationError, match="original Cloudflare account"):
        inspect(uncertain, owner, rotated)
    assert rotated.list_zones.call_count == count and not DomainDNSRecoveryEvidence.objects.exists()


def test_rotating_again_requires_new_evidence_and_cannot_be_silently_accepted(uncertain, owner, rotated):
    evidence = inspect(uncertain, owner, rotated)
    rotated.token, rotated.headers["Authorization"] = "NEXT-TOKEN", "Bearer NEXT-TOKEN"
    with pytest.raises(ValidationError, match="observation"):
        resolve(uncertain, owner, rotated, evidence)
    assert not uncertain[1].receipt_digest


def test_separate_owner_evidence_retains_full_normal_inspection_history(uncertain, owner, rotated):
    step = uncertain[1]
    step.observations = step.observations * 20
    step.save(update_fields=["observations"])
    evidence = inspect(uncertain, owner, rotated)
    step.refresh_from_db()
    assert len(step.observations) == 20 and evidence.step_id == step.pk
    assert resolve(uncertain, owner, rotated, evidence).status == "complete"


def test_recovery_evidence_cannot_be_edited_deleted_or_rehashed(uncertain, owner, rotated):
    evidence = inspect(uncertain, owner, rotated)
    evidence.body = {"other": True}
    with pytest.raises(ValidationError, match="Retain"):
        evidence.save()
    with pytest.raises(ValidationError, match="history"):
        evidence.delete()
    DomainDNSRecoveryEvidence.objects.filter(pk=evidence.pk).update(body={"tampered": True})
    with pytest.raises(ValidationError, match="observation"):
        resolve(uncertain, owner, rotated, evidence)


def test_owner_revoked_during_inspection_cannot_publish_evidence(uncertain, owner, rotated, django_user_model):
    original = rotated.list_zones.side_effect
    def revoke(*args, **kwargs):
        value = original(*args, **kwargs)
        django_user_model.objects.filter(pk=owner.pk).update(is_active=False)
        return value
    rotated.list_zones.side_effect = revoke
    with pytest.raises(PermissionDenied):
        inspect(uncertain, owner, rotated)
    assert not DomainDNSRecoveryEvidence.objects.exists()
    assert DomainDNSReservation.objects.filter(change=uncertain[0][0][0]).exists()


def test_observation_audit_failure_rolls_back_evidence_and_retains_claim(uncertain, owner, rotated):
    create = AuditLog.objects.create
    def fail(*args, **kwargs):
        if kwargs.get("action") == "domain.dns.credential_evidence_observed":
            raise RuntimeError("audit unavailable")
        return create(*args, **kwargs)
    with patch("apps.domains.dns_credential_recovery.AuditLog.objects.create", side_effect=fail):
        with pytest.raises(RuntimeError):
            inspect(uncertain, owner, rotated)
    uncertain[1].refresh_from_db()
    assert not DomainDNSRecoveryEvidence.objects.exists() and uncertain[1].token


@pytest.mark.parametrize("field,value", [("evidence_id", None), ("evidence_id", "invalid"), ("evidence_digest", "wrong"), ("confirmed", "true")])
def test_recovery_requires_exact_immutable_evidence_and_typed_consent(uncertain, owner, rotated, field, value):
    evidence = inspect(uncertain, owner, rotated)
    with pytest.raises(ValidationError):
        resolve(uncertain, owner, rotated, evidence, **{field: value})


def test_scope_change_during_readback_cannot_complete_resolution(uncertain, owner, rotated):
    evidence = inspect(uncertain, owner, rotated)
    original = rotated.get_zone.side_effect
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        rotated.token, rotated.headers["Authorization"] = "NEXT-TOKEN", "Bearer NEXT-TOKEN"
        return result
    rotated.get_zone.side_effect = changed
    result = resolve(uncertain, owner, rotated, evidence)
    assert result.status == "review" and not result.receipt_digest and not result.provider_id


def test_completed_recovery_is_not_replayed_as_another_create(uncertain, owner, rotated):
    evidence = inspect(uncertain, owner, rotated)
    result = resolve(uncertain, owner, rotated, evidence)
    assert execute(uncertain[0]).receipt_digest == result.receipt_digest
    rotated.create_managed_zone.assert_not_called()


def test_absence_under_replacement_credentials_never_authorizes_retry(uncertain, owner, rotated):
    uncertain[0][1]["exists"] = False
    evidence = inspect(uncertain, owner, rotated)
    assert evidence.body["zone"] is None and evidence.body["retry_authorized"] is False
    with pytest.raises(ValidationError, match="observation"):
        resolve(uncertain, owner, rotated, evidence)
    assert uncertain[0][0][2].create_managed_zone.call_count == 1
    rotated.create_managed_zone.assert_not_called()


def test_owner_revoked_during_resolution_cannot_publish_association(uncertain, owner, rotated, django_user_model):
    evidence = inspect(uncertain, owner, rotated)
    original = rotated.list_zones.side_effect
    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        django_user_model.objects.filter(pk=owner.pk).update(is_active=False)
        return result
    rotated.list_zones.side_effect = revoked
    result = resolve(uncertain, owner, rotated, evidence)
    assert result.status == "review" and not result.receipt_digest and not result.provider_id


def test_missing_owner_evidence_cannot_be_substituted_with_normal_observation(uncertain, owner, rotated):
    evidence = inspect(uncertain, owner, rotated)
    with pytest.raises(PermissionDenied):
        resolve(uncertain, owner, rotated, evidence, evidence_id="00000000-0000-0000-0000-000000000000")
