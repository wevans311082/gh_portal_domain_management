import uuid
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.identity_review import record_local_review
from apps.domains.models import ContactVerification
from apps.domains.validation_summary import contact_validation_summary
from tests.security_helpers import login_owner
from tests.test_contact_change_recovery import owner
from tests.test_identity_review_views import payload, url
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


def organisation(order, country="US"):
    contact = order.registration_contact
    contact.registrant_type = "organisation"
    contact.company = "Example Trading"
    contact.company_number = ""
    contact.country = country
    contact.save()
    return contact


def review(order, owner, **kwargs):
    values = dict(actor=owner, contact_id=order.registration_contact_id, check_kind="business",
        key=uuid.uuid4(), subject_digest=ContactVerification.digest_for(order.registration_contact),
        status="verified", policy_reference="LOCAL-BUSINESS-v2", evidence_reference="CASE-B100",
        valid_days=30, policy_reviewed=True)
    values.update(kwargs)
    return record_local_review(**values)


@pytest.mark.parametrize("country", ["US", "GB"])
def test_sole_trader_or_overseas_organisation_needs_no_company_number(order, owner, country):
    contact = organisation(order, country)
    with patch("apps.companies.services.CompaniesHouseService") as companies, patch(
        "apps.domains.resellerclub_client.ResellerClubClient") as provider:
        evidence = review(order, owner)
    companies.assert_not_called()
    provider.assert_not_called()
    assert evidence.is_verified and evidence.check_kind == "business"
    assert contact_validation_summary(contact)["business"] == "Verified"
    assert not ContactVerification.objects.exclude(check_kind="business").exists()
    audit = AuditLog.objects.get(action="domain.contact.business_reviewed")
    assert audit.data == {"verification_id": evidence.pk, "status": "verified"}


def test_individual_cannot_acquire_a_business_review(order, owner):
    with pytest.raises(ValidationError, match="Individuals"):
        review(order, owner)
    assert not ContactVerification.objects.exists()


@pytest.mark.parametrize("kind", ["registrar_email", "eligibility", "unknown"])
def test_local_review_cannot_record_external_check_types(order, owner, kind):
    with pytest.raises(ValidationError):
        review(order, owner, check_kind=kind)
    assert not ContactVerification.objects.exists()


def test_new_business_outcome_supersedes_companies_house_without_changing_identity(order, owner):
    contact = organisation(order)
    old = ContactVerification.objects.create(contact=contact, check_kind="business", status="verified",
        source="companies_house", reference="12345678", subject_digest=ContactVerification.digest_for(contact),
        checked_at=timezone.now())
    identity = review(order, owner, check_kind="identity")
    latest = review(order, owner, status="rejected")
    assert not old.is_current and identity.is_verified and latest.is_current
    assert contact_validation_summary(contact) == dict(identity="Verified", business="Rejected",
        eligibility=[], eligibility_truncated=False)


def test_business_edit_invalidates_review_and_replay_cannot_change_scope(order, owner):
    contact = organisation(order)
    evidence = review(order, owner)
    assert review(order, owner, key=evidence.review_key).pk == evidence.pk
    with pytest.raises(ValidationError, match="another outcome"):
        review(order, owner, key=evidence.review_key, check_kind="identity")
    contact.company = "Changed Trading"
    contact.save()
    assert not evidence.is_current


def test_business_audit_failure_rolls_back_evidence(order, owner):
    organisation(order)
    with patch.object(AuditLog.objects, "create", side_effect=DatabaseError), pytest.raises(DatabaseError):
        review(order, owner)
    assert not ContactVerification.objects.exists()


def test_owner_form_offers_business_scope_only_for_organisations(order, owner, client):
    login_owner(client, owner)
    response = client.get(url(order))
    assert list(response.context["form"].fields["check_kind"].choices) == [("identity", "Local identity review")]
    forged = dict(payload(order), check_kind="business")
    assert client.post(url(order), forged).status_code == 400
    organisation(order)
    response = client.get(url(order))
    assert "business" in dict(response.context["form"].fields["check_kind"].choices)
    data = dict(payload(order), check_kind="business")
    assert client.post(url(order), data).status_code == 302
    assert client.post(url(order), data).status_code == 302
    evidence = ContactVerification.objects.get()
    assert evidence.check_kind == "business" and evidence.is_verified
    response = client.get(url(order))
    assert b"CASE-100" in response.content and response["Cache-Control"] == "no-store"
