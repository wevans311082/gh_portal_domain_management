from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError

from apps.domains.models import DomainContact
from apps.domains.services import DomainContactService


def contact(**changes):
    fields = dict(
        registrant_type="individual",
        name="Jane Smith", company="", email="jane@example.com",
        phone_country_code="44", phone="07700 900000",
        address_line1="1 High Street", address_line2="", city="London",
        state="Greater London", postcode="SW1A 1AA", country="GB",
    )
    fields.update(changes)
    return DomainContact(**fields)


@pytest.mark.parametrize("attribute,value", [
    ("name", ""), ("address_line1", ""), ("city", ""), ("state", ""),
    ("postcode", ""), ("phone", ""), ("phone", "12"),
    ("phone", "0000000000"), ("phone", "07700900000 ext 123"),
    ("phone", "1234567890123"), ("phone_country_code", "abc"),
    ("country", "123"), ("country", "G1"), ("email", "invalid"),
    ("country", "ZZ"), ("country", "UK"), ("country", "EU"),
    ("country", "AN"), ("country", "XK"),
    ("phone_country_code", "999"), ("phone_country_code", "000"),
    ("address_line1", "a" * 65), ("postcode", "a" * 11),
    ("name", "Jane\nSmith"), ("company", "a" * 256),
])
def test_inaccurate_or_overlong_data_never_reaches_provider(attribute, value):
    provider = Mock()
    with pytest.raises(ValidationError) as error:
        DomainContactService(client=provider).sync_remote_contact(
            contact(**{attribute: value}), "999", "com"
        )
    assert attribute in error.value.message_dict
    provider.create_contact.assert_not_called()
    provider.update_contact.assert_not_called()


def test_individual_payload_keeps_real_details_without_company_registration():
    record = contact()
    payload = record.as_resellerclub_payload("999", "co.uk")
    assert payload["name"] == "Jane Smith"
    assert payload["company"] == "N/A"
    assert payload["phone"] == "7700900000"
    assert payload["address-line-1"] == "1 High Street"
    assert record.registrant_validation_status == "unvalidated"


@pytest.mark.parametrize("identifier", ["", "0", "-1", "999x", "1.5", "9" * 20])
def test_invalid_customer_identifier_is_rejected(identifier):
    with pytest.raises(ValidationError):
        contact().as_resellerclub_payload(identifier, "com")


def test_eu_field_limits_and_individual_company_value():
    eligibility = {"eu_eligibility_basis": "citizen", "eu_citizenship_country": "IE", "eu_eligibility_certified": True}
    assert contact(**eligibility).as_resellerclub_payload("999", "eu")["company"] == "NA"
    with pytest.raises(ValidationError) as error:
        contact(name="x" * 51, **eligibility).as_resellerclub_payload("999", "eu")
    assert "name" in error.value.message_dict




@pytest.mark.parametrize("result", [True, False, 1.5, {"contact_id": True}, {"contact_id": "private-diagnostic"}])
@pytest.mark.django_db
def test_contact_creation_rejects_boolean_or_invalid_identifiers(result):
    from apps.accounts.models import User
    owner = User.objects.create_user(email="binding@example.com", password="test-password")
    record = contact(user=owner)
    record.save()
    provider = Mock(create_contact=Mock(return_value=result))
    with pytest.raises(ValueError) as error:
        DomainContactService(client=provider).sync_remote_contact(record, "999", "com")
    assert not record.registrar_contact_id
    assert "private-diagnostic" not in str(error.value)


def test_default_contact_does_not_invent_phone():
    user = Mock(phone="", first_name="Jane", last_name="Smith", email="jane@example.com")
    with pytest.MonkeyPatch.context() as patch:
        manager = Mock()
        manager.filter.return_value.first.return_value = None
        patch.setattr("apps.domains.services.ClientProfile.objects", manager)
        assert DomainContactService().build_default_contact(user)["phone"] == ""
