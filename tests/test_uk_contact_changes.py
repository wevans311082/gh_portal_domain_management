from unittest.mock import Mock
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.domains.contact_changes import submit_contact_change
from apps.domains.models import DomainContactChange
from tests.test_contact_change_admission import current, request
from tests.test_contact_change_submission import api
from tests.test_registration_submission import order
from tests.test_contact_change_recovery import owner

pytestmark = pytest.mark.django_db


@pytest.fixture
def uk(current, api):
    domain, source = current
    domain.name, domain.tld = "personal.co.uk", "co.uk"
    domain.save()
    source.domain_name, source.tld = domain.name, domain.tld
    source.save()
    api.get_registration_reconciliation_details.return_value["domainname"] = domain.name
    api.get_contact = Mock(return_value={"entityid": "201", "customerid": "999", "type": "UkContact",
        "name": "Jane Smith", "company": "N/A"})
    return current, api


def roles(source):
    return {"registration_contact": source.registration_contact_id}


def test_uk_address_update_preserves_live_identity_and_only_registrant_role(uk):
    current, api = uk
    contact = current[1].registration_contact
    contact.address_line1 = "2 New Street"
    contact.save()
    change = request(current, contacts=roles(current[1]))
    assert submit_contact_change(change.pk, client=api) == "awaiting"
    assert api.get_contact.call_count == 2
    assert api.create_contact.call_args.args[0]["type"] == "UkContact"
    sent = api.change_domain_contacts.call_args.args[1]
    assert sent["reg"] == "301"
    assert sent["admin"] == sent["tech"] == sent["billing"] == "-1"
    current[1].refresh_from_db()
    assert current[1].registrar_contact_ids["reg"] == "201"


@pytest.mark.parametrize("changes", [{"name": "Another Holder"}, {"entityid": "202"}, {"customerid": "888"},
    {"type": "Contact"}, {"company": "Actual Limited"}, {"company": None}, {"status": "ERROR"}])
def test_unmatched_uk_identity_stops_before_contact_creation(uk, changes):
    current, api = uk
    api.get_contact.return_value.update(changes)
    change = request(current, contacts=roles(current[1]))
    with pytest.raises(ValidationError):
        submit_contact_change(change.pk, client=api)
    api.create_contact.assert_not_called()
    api.change_domain_contacts.assert_not_called()
    assert DomainContactChange.objects.get(pk=change.pk).status == "failed"


def test_editing_local_old_contact_cannot_authorize_uk_holder_change(uk):
    current, api = uk
    contact = current[1].registration_contact
    contact.name = "Replacement Holder"
    contact.save()
    change = request(current, contacts=roles(current[1]))
    with pytest.raises(ValidationError, match="registrant-transfer"):
        submit_contact_change(change.pk, client=api)
    api.create_contact.assert_not_called()


def test_organization_cannot_silently_become_individual(uk):
    current, api = uk
    api.get_contact.return_value["company"] = "Original Limited"
    change = request(current, contacts=roles(current[1]))
    with pytest.raises(ValidationError, match="registrant-transfer"):
        submit_contact_change(change.pk, client=api)
    api.change_domain_contacts.assert_not_called()


def test_provider_holder_edit_during_preparation_prevents_uk_submission(uk):
    current, api = uk
    old = dict(api.get_contact.return_value)
    api.get_contact.side_effect = [old, {**old, "name": "Changed Live Holder"}]
    change = request(current, contacts=roles(current[1]))
    with pytest.raises(ValidationError, match="registrant-transfer"):
        submit_contact_change(change.pk, client=api)
    api.change_domain_contacts.assert_not_called()
    assert DomainContactChange.objects.get(pk=change.pk).status == "unknown"


@pytest.mark.parametrize("new_company,allowed", [("Original Limited", True), ("Replacement Limited", False)])
def test_organization_update_requires_same_live_holder(uk, new_company, allowed):
    current, api = uk
    contact = current[1].registration_contact
    contact.registrant_type, contact.company = "organisation", new_company
    contact.save()
    api.get_contact.return_value["company"] = "Original Limited"
    change = request(current, contacts=roles(current[1]))
    if allowed:
        assert submit_contact_change(change.pk, client=api) == "awaiting"
    else:
        with pytest.raises(ValidationError, match="registrant-transfer"):
            submit_contact_change(change.pk, client=api)
        api.create_contact.assert_not_called()


def test_confirmed_me_uk_update_becomes_current_renewal_snapshot(uk, owner):
    from apps.domains.contact_change_confirmation import ContactChangeAction
    from apps.domains.contact_change_recovery import confirm_provider_completed
    from apps.domains.models import ContactVerification
    from apps.domains.registry_requirements import validate_renewal_registrant
    current, api = uk
    domain, source = current
    domain.name = source.domain_name = "personal.me.uk"
    domain.tld = source.tld = "me.uk"
    domain.save()
    source.save()
    api.get_registration_reconciliation_details.return_value["domainname"] = domain.name
    contact = source.registration_contact
    original_digest = ContactVerification.digest_for(contact)
    attempt = source.registration_attempt
    attempt.subject = {"contacts": {str(contact.pk): original_digest}}
    attempt.save()
    contact.address_line1 = "2 Updated Street"
    contact.save()
    with pytest.raises(ValidationError, match="changed since registrar submission"):
        validate_renewal_registrant(domain)
    change = request(current, contacts=roles(source))
    submit_contact_change(change.pk, client=api)
    stamp = timezone.now() - timedelta(minutes=20)
    DomainContactChange.objects.filter(pk=change.pk).update(submitted_at=stamp)
    api.get_contact_change_action.return_value = ContactChangeAction("501", "701", "ModifyContact", "ReviewedProviderStatus",
        str(int((stamp + timedelta(minutes=1)).timestamp())))
    api.get_registration_reconciliation_details.return_value["registrantcontactid"] = "301"
    assert confirm_provider_completed(change.pk, owner_id=owner.pk, reference="CASE-701",
        reason="Provider confirmed this exact UK update completed successfully.", provider_success_confirmed=True, client=api) == "confirmed"
    validate_renewal_registrant(domain)
    attempt.refresh_from_db()
    assert attempt.subject["contacts"][str(contact.pk)] == original_digest
    api.get_irtp_status.assert_not_called()
    contact.address_line1 = "3 Unsynchronized Street"
    contact.save()
    with pytest.raises(ValidationError, match="changed since registrar confirmation"):
        validate_renewal_registrant(domain)
