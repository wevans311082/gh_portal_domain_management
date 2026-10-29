from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains import agreements
from apps.domains.agreements import publish_agreement
from apps.domains.models import DomainAgreementAcceptance, DomainAgreementAcceptanceDocument, DomainOrder, RegistrarAgreementVersion
from apps.domains.registration_submission import submit_registration
from apps.domains.transfer_submission import submit_transfer
from tests.agreement_helpers import attach_agreement
from tests.test_registration_submission import order
from tests.test_transfer_submission import transfer
from tests.test_registrar_agreements import owner, publication

pytestmark = pytest.mark.django_db
NS = ["ns1.example.com", "ns2.example.com"]


@pytest.fixture(params=["registration", "transfer"])
def target(request):
    return request.getfixturevalue("order" if request.param == "registration" else "transfer")


def operation(target):
    return "registration" if isinstance(target, DomainOrder) else "transfer"


def submit(target, provider):
    method = submit_registration if isinstance(target, DomainOrder) else submit_transfer
    return method(target.pk, "999", NS, client=provider)


def attempt_for(target):
    return target.registration_attempt if isinstance(target, DomainOrder) else target.submission_attempt


@pytest.fixture
def provider():
    return Mock(create_contact=Mock(return_value="201"), register_domain=Mock(return_value={"entityid": "501"}),
                transfer_domain=Mock(return_value={"entityid": "501"}))


def test_missing_acceptance_never_creates_contacts_or_paid_call(target, provider):
    with pytest.raises(ValidationError):
        submit(target, provider)
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()
    provider.transfer_domain.assert_not_called()
    assert not hasattr(target, "registration_attempt" if isinstance(target, DomainOrder) else "submission_attempt")


def test_changed_terms_require_new_customer_acceptance_before_any_provider_mutation(target, provider, owner):
    attach_agreement(target)
    publish_agreement(actor=owner, **publication(version="new-paid-terms"))
    with pytest.raises(ValidationError, match="Current customer acceptance"):
        submit(target, provider)
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()
    provider.transfer_domain.assert_not_called()


def test_terms_change_during_contact_preparation_aborts_before_paid_marker(target, provider, owner):
    attach_agreement(target)
    def create(payload):
        publish_agreement(actor=owner, **publication(version="during-preparation"))
        return "201"
    provider.create_contact.side_effect = create
    with pytest.raises(ValidationError, match="Current customer acceptance"):
        submit(target, provider)
    attempt = attempt_for(target)
    assert attempt.status == "aborted" and attempt.submitted_at is None
    provider.register_domain.assert_not_called()
    provider.transfer_domain.assert_not_called()


def test_later_publication_does_not_rewrite_admitted_evidence_or_repeat_purchase(target, provider, owner):
    attach_agreement(target)
    receipt_id = target.agreement_acceptance_id
    original = AuditLog.objects.create
    def audit(**kwargs):
        result = original(**kwargs)
        if kwargs["action"] == "domain." + operation(target) + ".submitting":
            publish_agreement(actor=owner, **publication(version="after-admission"))
        return result
    with patch.object(AuditLog.objects, "create", side_effect=audit):
        assert submit(target, provider) == "501"
    assert submit(target, provider) == "501"
    attempt = attempt_for(target)
    assert attempt.subject["agreement"]["acceptance_id"] == receipt_id
    assert attempt.subject["agreement"]["documents"][0]["version"] == "paid-fixture-v1"
    method = provider.register_domain if isinstance(target, DomainOrder) else provider.transfer_domain
    assert method.call_count == 1


def test_current_catalogue_is_not_consulted_during_provider_call_stage(target, provider):
    attach_agreement(target)
    with patch.object(agreements, "_catalogue_lock", wraps=agreements._catalogue_lock) as catalogue:
        def paid(**kwargs):
            assert catalogue.call_count == 2  # Claim and durable-marker transactions only.
            return {"entityid": "501"}
        provider.register_domain.side_effect = provider.transfer_domain.side_effect = paid
        assert submit(target, provider) == "501"
        assert catalogue.call_count == 2


def test_receipt_cannot_predate_its_documents_before_contact_creation(target, provider):
    attach_agreement(target)
    DomainAgreementAcceptance.objects.filter(pk=target.agreement_acceptance_id).update(accepted_at=timezone.now() - timedelta(days=1))
    with pytest.raises(ValidationError, match="acceptance record requires review"):
        submit(target, provider)
    provider.create_contact.assert_not_called()


def test_frozen_receipt_validation_reads_no_current_plan_after_profile_retirement(target, provider, monkeypatch):
    attach_agreement(target)
    assert submit(target, provider) == "501"
    attempt = attempt_for(target)
    from apps.domains.registrar_policy import PROFILE_TLDS
    monkeypatch.delitem(PROFILE_TLDS, "general_raa")
    with patch.object(agreements, "current_agreement_plan", side_effect=AssertionError("must not consult publication head")), \
            patch.object(agreements, "_catalogue_lock", side_effect=AssertionError("must not lock publication catalogue")):
        assert agreements.assert_acceptance_frozen(snapshot=attempt.subject["agreement"],
            receipt_id=target.agreement_acceptance_id, customer_id=target.user_id, contact=target.registration_contact,
            domain_name=target.domain_name, operation=operation(target), admitted_at=attempt.submitted_at) == attempt.subject["agreement"]


@pytest.mark.parametrize("damage", ["receipt", "links", "document", "snapshot", "contact", "manifest", "future_receipt"])
def test_damage_after_marker_requires_review_without_paid_post(target, provider, damage):
    attach_agreement(target)
    original = AuditLog.objects.create
    def audit(**kwargs):
        result = original(**kwargs)
        if kwargs["action"] == "domain." + operation(target) + ".submitting":
            if damage == "receipt":
                type(target).objects.filter(pk=target.pk).update(agreement_acceptance=None)
            elif damage == "links":
                DomainAgreementAcceptanceDocument.objects.filter(acceptance_id=target.agreement_acceptance_id).delete()
            elif damage == "document":
                RegistrarAgreementVersion.objects.filter(version="paid-fixture-v1").update(body="Damaged text")
            elif damage == "contact":
                type(target.registration_contact).objects.filter(pk=target.registration_contact_id).update(name="Changed holder")
            elif damage == "future_receipt":
                DomainAgreementAcceptance.objects.filter(pk=target.agreement_acceptance_id).update(accepted_at=timezone.now() + timedelta(hours=1))
            else:
                attempt = attempt_for(target)
                subject = deepcopy(attempt.subject)
                if damage == "manifest":
                    subject["agreement"]["documents"][0]["version"] = "forged-version"
                else:
                    subject.pop("agreement")
                type(attempt).objects.filter(pk=attempt.pk).update(subject=subject)
        return result
    with patch.object(AuditLog.objects, "create", side_effect=audit), pytest.raises(ValidationError):
        submit(target, provider)
    target.refresh_from_db()
    attempt = attempt_for(target)
    assert attempt.status == "unknown" and attempt.submitted_at is not None
    provider.register_domain.assert_not_called()
    provider.transfer_domain.assert_not_called()
