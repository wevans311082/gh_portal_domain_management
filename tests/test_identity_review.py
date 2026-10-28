import uuid
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError

from apps.audit.models import AuditLog
from apps.domains.identity_review import record_identity_review
from apps.domains.models import ContactVerification
from tests.test_contact_change_recovery import owner
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


def review(order, owner, **kwargs):
    values = dict(actor=owner, contact_id=order.registration_contact_id, key=uuid.uuid4(),
        subject_digest=ContactVerification.digest_for(order.registration_contact), status="verified",
        policy_reference="POLICY-IDENTITY-v1", evidence_reference="CASE-100", valid_days=90, policy_reviewed=True)
    values.update(kwargs)
    return record_identity_review(**values)


def test_individual_can_receive_local_review_without_business_or_provider_calls(order, owner):
    with patch("apps.companies.services.CompaniesHouseService") as companies, patch(
        "apps.domains.resellerclub_client.ResellerClubClient") as provider:
        evidence = review(order, owner)
    companies.assert_not_called()
    provider.assert_not_called()
    assert evidence.is_current and evidence.status == "verified" and evidence.reviewer == owner
    assert evidence.source == "local-policy-review" and evidence.policy_reference == "POLICY-IDENTITY-v1"
    assert not ContactVerification.objects.exclude(check_kind="identity").exists()
    assert not order.registration_contact.company_number
    assert "CASE-100" not in str(AuditLog.objects.get(action="domain.contact.identity_reviewed").data)


def test_repeated_review_key_is_idempotent_and_changed_outcome_is_rejected(order, owner):
    key = uuid.uuid4()
    evidence = review(order, owner, key=key)
    assert review(order, owner, key=key).pk == evidence.pk
    with pytest.raises(ValidationError, match="another outcome"):
        review(order, owner, key=key, status="rejected")
    assert ContactVerification.objects.count() == 1


def test_stale_contact_version_cannot_acquire_review(order, owner):
    original = ContactVerification.digest_for(order.registration_contact)
    contact = order.registration_contact
    contact.email = "new@example.test"
    contact.save()
    with pytest.raises(ValidationError, match="changed"):
        review(order, owner, subject_digest=original)
    assert not ContactVerification.objects.exists()


def test_non_owner_cannot_record_identity_review(order):
    with pytest.raises(PermissionDenied):
        review(order, order.user)


@pytest.mark.parametrize("values", [{"policy_reviewed": False}, {"policy_reference": ""},
    {"evidence_reference": "person@example.test"}, {"valid_days": 0}, {"valid_days": 366},
    {"status": "registrar_verified"}])
def test_review_requires_explicit_bounded_policy_metadata(order, owner, values):
    with pytest.raises(ValidationError):
        review(order, owner, **values)


def test_audit_failure_rolls_back_review_evidence(order, owner):
    with patch.object(AuditLog.objects, "create", side_effect=DatabaseError), pytest.raises(DatabaseError):
        review(order, owner)
    assert not ContactVerification.objects.exists()
