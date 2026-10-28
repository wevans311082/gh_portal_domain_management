import pytest
from django.utils import timezone

from apps.domains.models import ContactVerification
from apps.domains.validation_summary import contact_validation_summary, domain_eligibility_summary
from tests.test_contact_change_admission import current
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


def evidence(contact, kind, **kwargs):
    values = dict(contact=contact, check_kind=kind, status="verified", source="reviewed-source",
        reference="private-evidence-reference", subject_digest=ContactVerification.digest_for(contact), checked_at=timezone.now())
    values.update(kwargs)
    return ContactVerification.objects.create(**values)


def test_individual_identity_is_independent_from_business_and_eligibility(order):
    contact = order.registration_contact
    evidence(contact, "identity")
    summary = contact_validation_summary(contact)
    assert summary["identity"] == "Verified"
    assert summary["business"] == "Not applicable"
    assert summary["eligibility"] == []
    assert "private-evidence-reference" not in str(summary)


def test_contact_edit_makes_identity_and_eligibility_stale(order):
    contact = order.registration_contact
    evidence(contact, "identity")
    evidence(contact, "eligibility", tld="com")
    contact.email = "changed@example.test"
    contact.save()
    summary = contact_validation_summary(contact)
    assert summary["identity"] == summary["eligibility"][0]["label"] == "Needs recheck"


def test_unscoped_eligibility_cannot_be_displayed_as_verified(order):
    contact = order.registration_contact
    evidence(contact, "eligibility")
    assert contact_validation_summary(contact)["eligibility"][0]["label"] == "Scope requires review"


def test_contact_level_eligibility_does_not_verify_domain(current):
    domain, source = current
    evidence(source.registration_contact, "eligibility", tld="com")
    assert domain_eligibility_summary(domain) == "No independent eligibility result recorded"


def test_domain_eligibility_requires_exact_current_association(current):
    domain, source = current
    proof = evidence(source.registration_contact, "eligibility", tld="com", domain_name=domain.name,
        domain_order=source, reference=domain.registrar_id, remote_contact_id="201", provider_scope=source.registrar_contact_ids["_scope"])
    assert domain_eligibility_summary(domain) == "Verified"
    proof.remote_contact_id = "9999"
    proof.save()
    assert domain_eligibility_summary(domain) == "No independent eligibility result recorded"


def test_many_eligibility_scopes_are_explicitly_truncated(order):
    for index in range(6):
        evidence(order.registration_contact, "eligibility", tld=f"scope{index}")
    summary = contact_validation_summary(order.registration_contact)
    assert len(summary["eligibility"]) == 3 and summary["eligibility_truncated"]
