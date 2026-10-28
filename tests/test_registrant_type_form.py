from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.domains.forms import DomainContactForm, DomainRegistrationForm
from apps.domains.models import ContactVerification, DomainContact


def data(**changes):
    values = dict(
        label="Home", registrant_type="individual", name="Jane Smith",
        company="", company_number="", email="jane@example.com",
        phone_country_code="44", phone="07700900000", address_line1="1 High Street",
        address_line2="", city="London", state="London", postcode="SW1A 1AA",
        country="GB",
    )
    values.update(changes)
    return values


@pytest.mark.django_db
@pytest.mark.parametrize("changes,field", [
    ({"registrant_type": ""}, "registrant_type"),
    ({"company": "Trading Name"}, "registrant_type"),
    ({"registrant_type": "organisation"}, "company"),
    ({"phone": "123"}, "phone"),
    ({"address_line1": "x" * 65}, "address_line1"),
    ({"validate_company_with_companies_house": "on"}, "validate_company_with_companies_house"),
])
def test_invalid_details_rejected_before_business_api(changes, field):
    with patch("apps.domains.forms.CompaniesHouseService") as provider:
        form = DomainContactForm(data(**changes))
        assert not form.is_valid()
        assert field in form.errors
        provider.assert_not_called()


@pytest.mark.django_db
def test_individual_is_saved_without_business_or_registrar_approval(client, django_user_model):
    user = django_user_model.objects.create_user(email="person@example.com", password="testpass")
    client.force_login(user)
    with patch("apps.domains.forms.CompaniesHouseService") as provider:
        response = client.post(reverse("domains:contact_create"), data())
        provider.assert_not_called()
    assert response.status_code == 302
    record = user.domain_contacts.get()
    assert record.registrant_type == "individual"
    assert record.registrant_validation_status == "unvalidated"
    assert not record.verification_evidence.exists()
    form = DomainRegistrationForm({"contact": record.pk}, user=user)
    # Other required fields are absent, but the individual contact is eligible
    # for the next registration checks without premature email verification.
    form.is_valid()
    assert "contact" not in form.errors


@pytest.mark.django_db
def test_business_check_records_only_matching_business_evidence(client, django_user_model):
    user = django_user_model.objects.create_user(email="business@example.com", password="testpass")
    client.force_login(user)
    with patch("apps.domains.forms.CompaniesHouseService") as provider:
        provider.return_value.get_company.return_value = {
            "company_number": "12345678", "company_name": "EXAMPLE LIMITED",
        }
        response = client.post(reverse("domains:contact_create"), data(
            registrant_type="organisation", company="Example Limited",
            company_number="12345678", validate_company_with_companies_house="on",
        ))
    assert response.status_code == 302
    record = user.domain_contacts.get()
    result = record.verification_evidence.get()
    assert result.check_kind == "business"
    assert result.is_verified
    assert record.registrant_validation_status == "unvalidated"
    assert not ContactVerification.objects.filter(check_kind="registrar_email").exists()


@pytest.mark.django_db
def test_wrong_company_record_cannot_be_approved():
    with patch("apps.domains.forms.CompaniesHouseService") as provider:
        provider.return_value.get_company.return_value = {
            "company_number": "12345678", "company_name": "Different Company",
        }
        form = DomainContactForm(data(
            registrant_type="organisation", company="Example Limited",
            company_number="12345678", validate_company_with_companies_house="on",
        ))
        assert not form.is_valid()
        assert "company" in form.errors


@pytest.mark.django_db
def test_legacy_contact_requires_explicit_type_confirmation(client, django_user_model):
    user = django_user_model.objects.create_user(email="legacy@example.com", password="testpass")
    record = DomainContact.objects.create(user=user, **{
        key: value for key, value in data().items() if key != "registrant_type"
    })
    form = DomainRegistrationForm({"contact": record.pk}, user=user)
    assert not form.is_valid()
    assert "contact" in form.errors
    client.force_login(user)
    response = client.get(reverse("domains:contact_edit", kwargs={"pk": record.pk}))
    assert response.status_code == 200
    assert response.context["form"]["registrant_type"].value() == ""
    assert b"Individuals do not need a company number" in response.content


@pytest.mark.django_db
def test_type_change_invalidates_previous_evidence(django_user_model):
    from django.utils import timezone
    user = django_user_model.objects.create_user(email="change@example.com", password="testpass")
    record = DomainContact.objects.create(user=user, **data())
    result = ContactVerification.objects.create(
        contact=record, check_kind="identity", status="verified", source="test",
        subject_digest=ContactVerification.digest_for(record), checked_at=timezone.now(),
    )
    DomainContact.objects.filter(pk=record.pk).update(registrant_type="organisation")
    assert not result.is_verified
