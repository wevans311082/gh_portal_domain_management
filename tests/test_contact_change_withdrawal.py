import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.domains.contact_changes import submit_contact_change
from apps.domains.models import DomainContactChange
from apps.domains.publication_consent import withdraw_decision
from apps.support.models import SupportTicket
from tests.test_contact_change_admission import current, request
from tests.test_contact_change_submission import api
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


def proposal(current):
    contact = current[1].registration_contact
    contact.registrant_type, contact.company = "organisation", "Example Limited"
    contact.save()
    return request(current, publication_choice="publish")


@pytest.mark.parametrize("status", ["submitting", "unknown", "awaiting", "confirmed"])
def test_submitted_contact_publication_withdrawal_creates_one_tracked_case(current, status):
    change = proposal(current)
    DomainContactChange.objects.filter(pk=change.pk).update(status=status, submitted_at=timezone.now())
    first = withdraw_decision(current[1].user, change.publication_decision_id)
    second = withdraw_decision(current[1].user, change.publication_decision_id)
    assert first.withdrawal_ticket_id == second.withdrawal_ticket_id
    assert SupportTicket.objects.count() == 1
    assert "contact replacement" in SupportTicket.objects.get().messages.get().content


def test_unsubmitted_contact_consent_withdrawal_stops_publication_before_provider_work(current, api):
    change = proposal(current)
    result = withdraw_decision(current[1].user, change.publication_decision_id)
    assert result.withdrawal_ticket_id is None
    with pytest.raises(ValidationError, match="publication choice changed"):
        submit_contact_change(change.pk, client=api)
    api.create_contact.assert_not_called()
    api.change_domain_contacts.assert_not_called()
