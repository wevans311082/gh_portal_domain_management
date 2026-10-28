from datetime import timedelta

import pytest
from django.utils import timezone

from apps.domains.models import ContactVerification, DomainContact


@pytest.fixture
def saved_contact(db, django_user_model):
    user = django_user_model.objects.create_user(email="evidence@example.com", password="testpass")
    return DomainContact.objects.create(
        user=user, label="Individual", name="Jane Smith", email=user.email,
        phone="7700900000", address_line1="1 High Street", city="London",
        state="London", postcode="SW1A 1AA", country="GB",
        registrant_validation_status="validated",
    )


def evidence(contact, **overrides):
    values = dict(
        contact=contact, check_kind="registrar_email", status="verified",
        subject_digest=ContactVerification.digest_for(contact),
        source="provider-test-fixture", checked_at=timezone.now(),
    )
    values.update(overrides)
    return ContactVerification.objects.create(**values)


def test_legacy_local_validation_creates_no_registrar_evidence(saved_contact):
    assert not saved_contact.verification_evidence.exists()


@pytest.mark.parametrize("attribute,value", [
    ("name", "Changed Name"), ("company", "New Company"),
    ("company_number", "12345678"), ("email", "changed@example.com"),
    ("phone_country_code", "1"), ("phone", "1234567890"),
    ("address_line1", "2 High Street"), ("address_line2", "Suite 2"),
    ("city", "Manchester"), ("state", "Lancashire"),
    ("postcode", "M1 1AA"), ("country", "US"),
    ("registrar_contact_id", "123"),
])
def test_persisted_change_invalidates_even_cached_evidence(saved_contact, attribute, value):
    result = evidence(saved_contact)
    assert result.is_verified
    DomainContact.objects.filter(pk=saved_contact.pk).update(**{attribute: value})
    assert not result.is_current
    assert not result.is_verified
    result.refresh_from_db()
    assert result.status == "verified"  # Historical evidence is retained.


def test_cosmetic_label_change_keeps_evidence_current(saved_contact):
    result = evidence(saved_contact)
    DomainContact.objects.filter(pk=saved_contact.pk).update(label="Home", is_default=True)
    assert result.is_verified


@pytest.mark.parametrize("changes", [
    {"expires_at": timezone.now() - timedelta(seconds=1)},
    {"checked_at": timezone.now() + timedelta(days=1)},
    {"status": "pending"}, {"status": "rejected"}, {"status": "unknown"},
])
def test_noncurrent_or_incomplete_checks_are_not_verified(saved_contact, changes):
    assert not evidence(saved_contact, **changes).is_verified


def test_business_check_does_not_create_email_or_eligibility_evidence(saved_contact):
    result = evidence(saved_contact, check_kind="business")
    assert result.is_verified
    assert not saved_contact.verification_evidence.filter(
        check_kind__in=["registrar_email", "eligibility"]
    ).exists()


@pytest.mark.parametrize("kind", ["identity", "business", "eligibility"])
@pytest.mark.parametrize("status", ["rejected", "unknown", "pending"])
def test_later_case_outcome_supersedes_older_approval_in_same_scope(saved_contact, kind, status):
    original = evidence(saved_contact, check_kind=kind, reference="CASE-OLD", tld="com")
    later = evidence(saved_contact, check_kind=kind, reference="CASE-NEW", tld="com", status=status)
    assert later.is_current
    assert not original.is_current and not original.is_verified
    original.refresh_from_db()
    assert original.status == "verified"  # Preserve the historical outcome.


def test_eligibility_for_other_domain_does_not_supersede_this_domain(saved_contact):
    first = evidence(saved_contact, check_kind="eligibility", domain_name="first.com", tld="com", reference="CASE-ONE")
    second = evidence(saved_contact, check_kind="eligibility", domain_name="second.com", tld="com", reference="CASE-TWO", status="rejected")
    assert first.is_verified and second.is_current


def test_expired_later_rejection_does_not_resurrect_prior_approval(saved_contact):
    original = evidence(saved_contact, check_kind="identity", reference="CASE-OLD")
    evidence(saved_contact, check_kind="identity", reference="CASE-NEW", status="rejected", expires_at=timezone.now() - timedelta(seconds=1))
    assert not original.is_current
