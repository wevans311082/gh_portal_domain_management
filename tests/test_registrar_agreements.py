import json
from io import StringIO
import uuid
from unittest.mock import patch
from dataclasses import replace

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command, CommandError
from django.db import DatabaseError

from apps.audit.models import AuditLog
from apps.domains.agreements import publish_agreement, current_agreement_plan, record_agreement_acceptance, assert_acceptance_current
from apps.domains.models import ContactVerification, RegistrarAgreementVersion, RegistrarAgreementBinding, DomainAgreementAcceptance
from apps.domains.registrar_policy import POLICIES
from tests.test_contact_change_recovery import owner
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


def publication(**kwargs):
    values = dict(submission_key=uuid.uuid4(), agreement_key="domain-service", version="test-v1", title="Test agreement",
        body="Test-only text, not a real legal agreement.\nExact <script>plain text</script> remains inert.",
        source_url="https://example.test/terms", profile_keys=["general_raa", "general_registry", "uk_accuracy", "us_nexus"],
        operations=["registration", "transfer"], reviewed=True)
    values.update(kwargs)
    return values


@pytest.fixture
def document(owner):
    return publish_agreement(actor=owner, **publication())


def accept(order, **kwargs):
    plan = current_agreement_plan(order.domain_name, "registration")
    values = dict(actor=order.user, contact_id=order.registration_contact_id, domain_name=order.domain_name,
        operation="registration", key=uuid.uuid4(), subject_digest=ContactVerification.digest_for(order.registration_contact),
        plan_digest=plan.digest, accepted=True)
    values.update(kwargs)
    return record_agreement_acceptance(**values)


def check(order, receipt, **kwargs):
    values = dict(receipt_id=receipt.pk, customer_id=order.user_id, contact_id=order.registration_contact_id,
        domain_name=order.domain_name, operation="registration")
    values.update(kwargs)
    return assert_acceptance_current(**values)


def test_publication_has_no_inferred_customer_acceptance_or_provider_calls(owner):
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as provider:
        document = publish_agreement(actor=owner, **publication())
    provider.assert_not_called()
    assert document.publisher == owner and len(document.document_digest) == 64
    assert RegistrarAgreementBinding.objects.count() == 8
    assert not DomainAgreementAcceptance.objects.exists() and not ContactVerification.objects.exists()
    assert document.body not in str(AuditLog.objects.get(action="domain.agreement.published").data)


@pytest.mark.parametrize("domain", ["example.com", "example.net", "example.org", "example.io", "example.uk", "example.me.uk", "example.us"])
def test_one_published_document_is_applied_through_shared_profile_lists(document, domain):
    plan = current_agreement_plan(domain, "registration")
    assert plan.documents == (document,) and len(plan.digest) == 64


@pytest.mark.parametrize("domain,operation", [("example.eu", "registration"), ("example.unknown", "registration"), ("example.co.uk", "transfer")])
def test_publication_never_enables_an_unsupported_domain_workflow(document, domain, operation):
    with pytest.raises(ValidationError):
        current_agreement_plan(domain, operation)


def test_missing_documents_require_review_instead_of_an_empty_approval(order):
    with pytest.raises(ValidationError, match="not been published"):
        current_agreement_plan(order.domain_name, "registration")


def test_publication_is_idempotent_and_does_not_reactivate_old_versions(owner):
    first = publication()
    original = publish_agreement(actor=owner, **first)
    updated = publish_agreement(actor=owner, **publication(version="test-v2"))
    assert publish_agreement(actor=owner, **first).pk == original.pk
    assert current_agreement_plan("example.com", "registration").documents == (updated,)
    with pytest.raises(ValidationError, match="another document"):
        publish_agreement(actor=owner, **dict(first, body="Changed text"))
    with pytest.raises(ValidationError, match="already exists"):
        publish_agreement(actor=owner, **publication(body="Replacement without a new version"))


def test_publishing_a_profile_subset_preserves_other_current_assignments(owner, document):
    newer = publish_agreement(actor=owner, **publication(version="uk-v2", profile_keys=["uk_accuracy"]))
    assert current_agreement_plan("example.com", "registration").documents == (document,)
    assert current_agreement_plan("example.co.uk", "registration").documents == (newer,)


def test_non_owner_cannot_publish_or_change_bindings(order):
    with pytest.raises(PermissionDenied):
        publish_agreement(actor=order.user, **publication())
    assert not RegistrarAgreementVersion.objects.exists() and not RegistrarAgreementBinding.objects.exists()


@pytest.mark.parametrize("values", [{"reviewed": False}, {"body": ""}, {"body": "x" * 20001},
    {"source_url": "http://example.test"}, {"source_url": "https://password@example.test/terms"},
    {"profile_keys": ["general_raa", "general_raa"]}, {"profile_keys": ["not-reviewed"]},
    {"operations": ["renewal"]}, {"submission_key": "bad"}])
def test_publication_rejects_unreviewed_or_malformed_input_without_side_effects(owner, values):
    with pytest.raises(ValidationError):
        publish_agreement(actor=owner, **publication(**values))
    assert not RegistrarAgreementVersion.objects.exists()


def test_audit_failure_rolls_back_publication_and_binding_switch(owner, document):
    with patch.object(AuditLog.objects, "create", side_effect=DatabaseError), pytest.raises(DatabaseError):
        publish_agreement(actor=owner, **publication(version="test-v2"))
    assert current_agreement_plan("example.com", "registration").documents == (document,)
    assert RegistrarAgreementVersion.objects.count() == 1


def test_exact_customer_acceptance_is_independent_from_verification(document, order):
    receipt = accept(order)
    snapshot = check(order, receipt)
    assert snapshot["acceptance_id"] == receipt.pk and receipt.documents.count() == 1
    assert snapshot["documents"][0]["digest"] == document.document_digest
    assert not ContactVerification.objects.exists()
    assert document.body not in str(AuditLog.objects.get(action="domain.agreement.accepted").data)


def test_acceptance_replay_never_silently_accepts_updated_terms(document, order, owner):
    receipt = accept(order)
    same = dict(actor=order.user, contact_id=order.registration_contact_id, domain_name=order.domain_name,
        operation="registration", key=receipt.key, subject_digest=receipt.subject_digest, plan_digest=receipt.plan_digest, accepted=True)
    assert record_agreement_acceptance(**same).pk == receipt.pk
    publish_agreement(actor=owner, **publication(version="test-v2"))
    assert record_agreement_acceptance(**same).pk == receipt.pk
    with pytest.raises(ValidationError, match="Current customer acceptance"):
        check(order, receipt)
    with pytest.raises(ValidationError, match="another request"):
        accept(order, key=receipt.key)
    assert DomainAgreementAcceptance.objects.count() == 1


def test_stale_displayed_plan_cannot_create_a_receipt(document, order, owner):
    old = current_agreement_plan(order.domain_name, "registration")
    publish_agreement(actor=owner, **publication(version="test-v2"))
    with pytest.raises(ValidationError, match="agreements changed"):
        accept(order, plan_digest=old.digest)
    assert not DomainAgreementAcceptance.objects.exists()


def test_contact_edit_invalidates_receipt_and_stale_form(document, order):
    receipt = accept(order)
    order.registration_contact.name = "Changed Customer"
    order.registration_contact.save()
    with pytest.raises(ValidationError):
        check(order, receipt)
    with pytest.raises(ValidationError, match="contact changed"):
        accept(order, subject_digest=receipt.subject_digest)


@pytest.mark.parametrize("values", [{"domain_name": "other.com"}, {"operation": "transfer"}])
def test_receipt_cannot_be_reused_for_another_domain_or_operation(document, order, values):
    receipt = accept(order)
    with pytest.raises(ValidationError):
        check(order, receipt, **values)


def test_staff_cannot_record_customer_acceptance(document, order, owner):
    with pytest.raises(PermissionDenied):
        accept(order, actor=owner)
    with pytest.raises(ValidationError, match="Explicitly accept"):
        accept(order, accepted=False)
    assert not DomainAgreementAcceptance.objects.exists()


def test_foreign_contact_cannot_be_accepted(document, order, django_user_model):
    other = django_user_model.objects.create_user(email="other-agreement@example.test", password="testpass")
    with pytest.raises(PermissionDenied):
        accept(order, actor=other)
    assert not DomainAgreementAcceptance.objects.exists()


def test_acceptance_audit_failure_rolls_back_receipt_and_document_links(document, order):
    with patch.object(AuditLog.objects, "create", side_effect=DatabaseError), pytest.raises(DatabaseError):
        accept(order)
    assert not DomainAgreementAcceptance.objects.exists() and not document.acceptance_documents.exists()


def test_multiple_required_agreements_must_all_be_available_and_accepted(document, order, owner, monkeypatch):
    monkeypatch.setitem(POLICIES, "com", replace(POLICIES["com"], agreement_keys=("domain-service", "registry-addendum")))
    with pytest.raises(ValidationError, match="not been published"):
        current_agreement_plan(order.domain_name, "registration")
    extra = publish_agreement(actor=owner, **publication(agreement_key="registry-addendum", profile_keys=["general_raa"]))
    receipt = accept(order)
    assert receipt.documents.count() == 2
    assert {entry["id"] for entry in check(order, receipt)["documents"]} == {document.pk, extra.pk}


def test_immutable_records_and_integrity_checks_preserve_original_text(document, order):
    receipt = accept(order)
    document.body = "Changed after publication"
    with pytest.raises(ValidationError, match="cannot be edited"):
        document.save()
    with pytest.raises(ValidationError, match="must be retained"):
        receipt.delete()
    RegistrarAgreementVersion.objects.filter(pk=document.pk).update(body="Unexpected database edit")
    with pytest.raises(ValidationError, match="requires owner review"):
        current_agreement_plan(order.domain_name, "registration")


def test_command_publishes_an_approved_file_without_printing_its_body(owner, tmp_path):
    data = publication()
    data.pop("reviewed")
    data["submission_key"] = str(data["submission_key"])
    path = tmp_path / "approved-test-agreement.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    output = StringIO()
    call_command("publish_registrar_agreement", owner_id=owner.pk, document_file=str(path), approved=True, stdout=output)
    assert "published" in output.getvalue() and data["body"] not in output.getvalue()
    assert not DomainAgreementAcceptance.objects.exists()


@pytest.mark.parametrize("raw", ['{"body":"first","body":"second"}', '["not an object"]', '{"unexpected":true}'])
def test_command_rejects_ambiguous_or_unknown_file_fields(owner, tmp_path, raw):
    path = tmp_path / "invalid-test-agreement.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(CommandError):
        call_command("publish_registrar_agreement", owner_id=owner.pk, document_file=str(path), approved=True)
    assert not RegistrarAgreementVersion.objects.exists()


def test_command_requires_explicit_approval_and_bounds_file_reads(owner, tmp_path):
    path = tmp_path / "oversized-test-agreement.json"
    path.write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(CommandError, match="Review and approve"):
        call_command("publish_registrar_agreement", owner_id=owner.pk, document_file=str(path))
    with pytest.raises(CommandError, match="at most 256 KiB"):
        call_command("publish_registrar_agreement", owner_id=owner.pk, document_file=str(path), approved=True)
    assert not RegistrarAgreementVersion.objects.exists()


def test_owner_access_is_reread_before_idempotent_publication(owner):
    values = publication()
    publish_agreement(actor=owner, **values)
    type(owner).objects.filter(pk=owner.pk).update(is_superuser=False)
    with pytest.raises(PermissionDenied):
        publish_agreement(actor=owner, **values)
    assert RegistrarAgreementVersion.objects.count() == 1


def test_receipt_document_links_cannot_be_dropped_without_invalidating_acceptance(document, order):
    receipt = accept(order)
    receipt.documents.all().delete()  # Simulate an out-of-band storage alteration.
    with pytest.raises(ValidationError, match="acceptance record requires review"):
        check(order, receipt)


def test_deactivated_customer_cannot_reuse_or_create_acceptance(document, order):
    receipt = accept(order)
    type(order.user).objects.filter(pk=order.user_id).update(is_active=False)
    with pytest.raises(ValidationError):
        check(order, receipt)
    with pytest.raises(PermissionDenied):
        accept(order)


def test_same_receipt_key_cannot_be_claimed_by_another_customer(document, order, django_user_model):
    receipt = accept(order)
    other = django_user_model.objects.create_user(email="other-receipt@example.test", password="testpass")
    clone = order.registration_contact
    clone.pk = None
    clone.user = other
    clone.save()
    with pytest.raises(ValidationError, match="another request"):
        accept(order, actor=other, contact_id=clone.pk, key=receipt.key,
            subject_digest=ContactVerification.digest_for(clone))
    assert DomainAgreementAcceptance.objects.count() == 1
