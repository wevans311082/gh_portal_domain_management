from unittest.mock import patch

import pytest

from apps.domains.contact_codes import CountryChoiceField
from apps.domains.forms import DomainContactForm
from tests.test_registrar_contact_accuracy import contact
from tests.test_registrant_type_form import data


@pytest.mark.parametrize("country", ["GB", "US", "GG", "JE", "IM", "AX", "TW", "PS"])
def test_assigned_countries_and_territories_are_supported(country):
    assert contact(country=country).as_resellerclub_payload("999", "com")["country"] == country


def test_address_country_and_telephone_calling_code_can_differ():
    payload = contact(country="US", city="New York", state="NY", postcode="10001", phone_country_code="44").as_resellerclub_payload("999", "com")
    assert payload["country"] == "US" and payload["phone-cc"] == "44"


@pytest.mark.parametrize("code", ["1", "44", "33", "358", "800"])
def test_assigned_calling_codes_include_shared_and_non_geographic_services(code):
    assert contact(phone_country_code=code).as_resellerclub_payload("999", "com")["phone-cc"] == code


def test_country_field_normalizes_case_and_preserves_selected_value_after_errors():
    field = CountryChoiceField()
    assert field.clean(" gb ") == "GB"
    assert field.prepare_value("gb") == "GB"
    form = DomainContactForm(data(country="gb", phone="12"))
    assert not form.is_valid()
    assert '<option value="GB" selected>' in str(form["country"])


@pytest.mark.parametrize("changes,field", [({"country": "ZZ"}, "country"), ({"phone_country_code": "999"}, "phone_country_code")])
def test_invalid_codes_are_rejected_before_optional_business_lookup(changes, field):
    with patch("apps.domains.forms.CompaniesHouseService") as company:
        form = DomainContactForm(data(registrant_type="organisation", company="Example Limited", company_number="12345678", validate_company_with_companies_house=True, **changes))
        assert not form.is_valid() and field in form.errors
        company.assert_not_called()


def test_country_field_errors_do_not_echo_unbounded_invalid_values():
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError) as exc:
        CountryChoiceField().clean("x" * 10000)
    assert "x" * 100 not in str(exc.value)
