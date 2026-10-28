import pytest

from apps.domains.models import PublicationRedaction
from apps.domains.publication_consent import capture_decision, withdraw_decision
from apps.domains.publication_redaction import process_publication_redaction
from tests.test_contact_change_admission import current
from tests.test_contact_change_submission import api
from tests.test_registration_submission import order
from tests.test_contact_change_recovery import owner

pytestmark = pytest.mark.django_db


@pytest.fixture
def redaction(current, api):
    source = current[1]
    contact = source.registration_contact
    contact.registrant_type, contact.company = "organisation", "Example Limited"
    contact.save()
    decision = capture_decision(customer=source.user, contact=contact, domain_name=source.domain_name, choice="publish")
    source.publication_decision = decision
    source.save()
    withdraw_decision(source.user, decision.pk)
    api.get_publication_reconciliation_details.return_value = {
        **api.get_registration_reconciliation_details.return_value,
        "registrantOrgConsentDetails": {"isRegistrantOrgConsentApplicable": "true", "registrantOrgConsent": "true"}}
    return PublicationRedaction.objects.get(decision=decision), api


def test_redaction_reuses_current_roles_and_requires_later_read_confirmation(redaction):
    job, api = redaction
    assert process_publication_redaction(job.pk, client=api) == "awaiting"
    sent = api.change_domain_contacts.call_args
    assert sent.args[1]["reg"] == sent.args[1]["admin"] == "201"
    assert sent.kwargs["registrant_org_consent"] is False
    api.create_contact.assert_not_called()
    assert process_publication_redaction(job.pk, client=api) == "awaiting"
    assert api.change_domain_contacts.call_count == 1
    api.get_publication_reconciliation_details.return_value["registrantOrgConsentDetails"]["registrantOrgConsent"] = "false"
    assert process_publication_redaction(job.pk, client=api) == "confirmed"
    assert PublicationRedaction.objects.get(pk=job.pk).confirmed_at is not None


def test_already_revoked_consent_needs_no_provider_mutation(redaction):
    job, api = redaction
    api.get_publication_reconciliation_details.return_value["registrantOrgConsentDetails"]["registrantOrgConsent"] = "false"
    assert process_publication_redaction(job.pk, client=api) == "confirmed"
    api.change_domain_contacts.assert_not_called()


def test_lost_redaction_response_never_repeats_mutation(redaction):
    job, api = redaction
    api.change_domain_contacts.side_effect = TimeoutError("private provider exception")
    with pytest.raises(TimeoutError):
        process_publication_redaction(job.pk, client=api)
    assert PublicationRedaction.objects.get(pk=job.pk).status == "unknown"
    assert process_publication_redaction(job.pk, client=api) == "unknown"
    assert api.change_domain_contacts.call_count == 1


def test_foreign_provider_roles_stop_before_mutation(redaction):
    from django.core.exceptions import ValidationError
    job, api = redaction
    api.get_publication_reconciliation_details.return_value["registrantcontactid"] = "9999"
    with pytest.raises(ValidationError):
        process_publication_redaction(job.pk, client=api)
    api.change_domain_contacts.assert_not_called()


def test_revocation_enqueue_is_idempotent(current, redaction):
    job, api = redaction
    withdraw_decision(current[1].user, job.decision_id)
    assert PublicationRedaction.objects.count() == 1


def test_withdrawn_inflight_contact_change_can_complete_then_redact(current, api, owner):
    from datetime import timedelta
    from django.core.exceptions import ValidationError
    from django.utils import timezone
    from apps.domains.contact_change_confirmation import ContactChangeAction
    from apps.domains.contact_change_recovery import confirm_provider_completed
    from apps.domains.contact_changes import submit_contact_change
    from apps.domains.models import DomainContactChange
    from tests.test_contact_change_admission import request
    source = current[1]
    contact = source.registration_contact
    contact.registrant_type, contact.company = "organisation", "Example Limited"
    contact.save()
    change = request(current, publication_choice="publish")
    submit_contact_change(change.pk, client=api)
    withdraw_decision(source.user, change.publication_decision_id)
    job = PublicationRedaction.objects.get(decision_id=change.publication_decision_id)
    with pytest.raises(ValidationError, match="stable registrar association"):
        process_publication_redaction(job.pk, client=api)
    stamp = timezone.now() - timedelta(minutes=20)
    DomainContactChange.objects.filter(pk=change.pk).update(submitted_at=stamp)
    api.get_contact_change_action.return_value = ContactChangeAction("501", "701", "ModifyContact", "ReviewedProviderStatus",
        str(int((stamp + timedelta(minutes=1)).timestamp())))
    api.get_irtp_status.return_value = {"order-id": "501", "domain-name": source.domain_name, "irtp-status": {
        "task-status": "SUCCESS", "losing-foa-status": "APPROVED", "gaining-foa-status": "APPROVED"}}
    for field in ("registrantcontactid", "admincontactid", "techcontactid", "billingcontactid"):
        api.get_registration_reconciliation_details.return_value[field] = "301"
    assert confirm_provider_completed(change.pk, owner_id=owner.pk, reference="CASE-701",
        reason="Provider confirmed this exact contact update completed successfully.", provider_success_confirmed=True, client=api) == "confirmed"
    api.get_publication_reconciliation_details.return_value = {**api.get_registration_reconciliation_details.return_value,
        "registrantOrgConsentDetails": {"isRegistrantOrgConsentApplicable": "true", "registrantOrgConsent": "true"}}
    assert process_publication_redaction(job.pk, client=api) == "awaiting"
    assert api.change_domain_contacts.call_args.kwargs["registrant_org_consent"] is False
    assert api.change_domain_contacts.call_count == 2 and api.create_contact.call_count == 1
    api.get_publication_reconciliation_details.return_value["registrantOrgConsentDetails"]["registrantOrgConsent"] = "false"
    assert process_publication_redaction(job.pk, client=api) == "confirmed"
