from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.admin_tools.manual_order_views import _ensure_contact
from apps.domains.contact_validation import validate_order_contacts
from apps.domains.models import DomainContact, DomainOrder
from tests.security_helpers import login_owner


@pytest.fixture
def customer(db, django_user_model):
    return django_user_model.objects.create_user(
        email="staff-contact@example.com", password="testpass", first_name="Jane", last_name="Smith"
    )


def supplied(**changes):
    values = dict(
        contact_registrant_type="individual", contact_phone="07700900000",
        contact_address="1 High Street", contact_city="London",
        contact_state="London", contact_postcode="SW1A 1AA",
    )
    values.update(changes)
    return values


def test_staff_can_create_individual_without_company(customer):
    record = _ensure_contact(customer, supplied(), "com")
    assert record.registrant_type == "individual"
    assert record.name == "Jane Smith"
    assert record.company == ""
    assert record.phone == "07700900000"
    assert record.registrant_validation_status == "unvalidated"
    assert not record.verification_evidence.exists()


@pytest.mark.parametrize("changes", [{"contact_country": "ZZ"}, {"contact_country": "UK"}, {"contact_phone_country_code": "999"}])
def test_staff_contact_rejects_unassigned_country_and_calling_codes(customer, changes):
    with pytest.raises(ValidationError):
        _ensure_contact(customer, supplied(**changes), "com")
    assert not DomainContact.objects.filter(user=customer).exists()


def test_staff_can_supply_overseas_organisation_without_uk_company_number(customer):
    record = _ensure_contact(customer, supplied(
        contact_registrant_type="organisation", contact_company="Example Trading",
        contact_country="US", contact_phone_country_code="1", contact_phone="2125550100",
        contact_address="10 Main Street", contact_city="New York",
        contact_state="NY", contact_postcode="10001",
    ), "com")
    assert record.country == "US"
    assert record.company_number == ""
    assert record.as_resellerclub_payload("999", "com")["phone"] == "2125550100"


@pytest.mark.parametrize("identifier", ["invalid", "1" * 100, "9223372036854775808", "-1"])
def test_invalid_selected_contact_ids_are_rejected_without_query(customer, identifier, django_assert_num_queries):
    with django_assert_num_queries(0), pytest.raises(ValidationError):
        _ensure_contact(customer, {"contact_id": identifier}, "com")


@pytest.mark.parametrize("changes", [
    {"contact_address": ""}, {"contact_phone": ""},
    {"contact_phone": "12"}, {"contact_city": ""},
    {"contact_state": ""}, {"contact_postcode": ""},
    {"contact_registrant_type": ""}, {"contact_address": "x" * 65},
])
def test_staff_cannot_create_contact_using_invented_defaults(customer, changes):
    with pytest.raises(ValidationError):
        _ensure_contact(customer, supplied(**changes), "com")
    assert not customer.domain_contacts.exists()


def test_staff_must_not_fall_back_from_an_unowned_selected_contact(customer, django_user_model):
    other = django_user_model.objects.create_user(email="other-contact@example.com", password="testpass")
    foreign = _ensure_contact(other, supplied(contact_name="Other Person"), "com")
    with pytest.raises(ValidationError):
        _ensure_contact(customer, supplied(contact_id=str(foreign.pk)), "com")
    assert not customer.domain_contacts.exists()


def test_staff_reuses_complete_owned_contact_and_does_not_mutate_for_override(customer):
    original = _ensure_contact(customer, supplied(), "com")
    assert _ensure_contact(customer, {}, "com").pk == original.pk
    replacement = _ensure_contact(customer, {"contact_phone": "07800900000"}, "com")
    assert replacement.pk != original.pk
    original.refresh_from_db()
    assert original.phone == "07700900000"


def test_legacy_unconfirmed_contact_cannot_bypass_worker_validation(customer):
    record = _ensure_contact(customer, supplied(), "com")
    record.registrant_type = "unspecified"
    record.save()
    with pytest.raises(ValidationError):
        _ensure_contact(customer, {}, "com")


@pytest.mark.parametrize("role", ["registration_contact", "admin_contact", "tech_contact", "billing_contact"])
def test_preflight_checks_every_role_before_any_provider_work(customer, role):
    good = _ensure_contact(customer, supplied(), "com")
    invalid = DomainContact.objects.get(pk=good.pk)
    invalid.phone = "12"
    values = {name: good for name in (
        "registration_contact", "admin_contact", "tech_contact", "billing_contact"
    )}
    values[role] = invalid
    with pytest.raises(ValidationError):
        validate_order_contacts(SimpleNamespace(user_id=customer.pk, domain_name="example.com", **values), "999", "com")


def test_preflight_rejects_cross_customer_contacts(customer):
    good = _ensure_contact(customer, supplied(), "com")
    order = SimpleNamespace(
        user_id=customer.pk + 1, domain_name="example.com", registration_contact=good,
        admin_contact=good, tech_contact=good, billing_contact=good,
    )
    with pytest.raises(ValidationError, match="belong"):
        validate_order_contacts(order, "999", "com")


def test_manual_order_http_returns_errors_without_order_or_registrar_call(customer, client, django_user_model):
    staff = django_user_model.objects.create_user(email="manual-staff@example.com", password="testpass", is_staff=True)
    login_owner(client, staff)
    with patch("apps.admin_tools.manual_order_views._load_whm_hosting_packages", return_value=([], "test")), patch(
        "apps.admin_tools.manual_order_views.register_domain_order"
    ) as registrar:
        response = client.post(reverse("admin_tools:manual_domain_register"), {
            "domain_name": "invalid-contact.com", "user_id": customer.pk, "years": "1",
            **supplied(contact_phone="12"),
        })
    assert response.status_code == 200
    assert response.context["contact_errors"]
    assert b"Complete the registrant details" in response.content
    assert not DomainOrder.objects.exists()
    assert not customer.domain_contacts.exists()
    registrar.assert_not_called()


def test_registration_worker_checks_last_role_before_first_external_change(customer, settings):
    from apps.domains.tasks import register_domain_order

    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    good = _ensure_contact(customer, supplied(), "com")
    bad = _ensure_contact(customer, supplied(contact_phone="07800900000"), "com")
    DomainContact.objects.filter(pk=bad.pk).update(phone="12")
    order = DomainOrder.objects.create(
        user=customer, domain_name="worker-preflight.com", tld="com", status="paid",
        registration_contact=good, admin_contact=good, tech_contact=good, billing_contact=bad,
    )
    provider = Mock()
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch(
        "apps.domains.tasks._build_nameservers", return_value=["ns1.example.com", "ns2.example.com"]
    ), patch.object(register_domain_order, "retry") as retry:
        with pytest.raises(ValidationError):
            register_domain_order.run(order.pk)
        retry.assert_not_called()
    provider.create_contact.assert_not_called()
    provider.update_contact.assert_not_called()
    provider.register_domain.assert_not_called()
    order.refresh_from_db()
    assert order.status == "failed"


def test_transfer_worker_checks_all_roles_before_first_external_change(customer, settings):
    from apps.domains.models import DomainTransfer
    from apps.domains.tasks import execute_domain_transfer

    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    good = _ensure_contact(customer, supplied(), "com")
    bad = _ensure_contact(customer, supplied(contact_phone="07800900000"), "com")
    DomainContact.objects.filter(pk=bad.pk).update(registrant_type="unspecified")
    transfer = DomainTransfer.objects.create(
        user=customer, domain_name="transfer-preflight.com", tld="com", status="paid",
        registration_contact=good, admin_contact=good, tech_contact=good, billing_contact=bad,
    )
    provider = Mock()
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch(
        "apps.domains.tasks._build_nameservers", return_value=["ns1.example.com", "ns2.example.com"]
    ):
        execute_domain_transfer.run(transfer.pk)
    provider.create_contact.assert_not_called()
    provider.update_contact.assert_not_called()
    provider.transfer_domain.assert_not_called()
    transfer.refresh_from_db()
    assert transfer.status == "failed"
