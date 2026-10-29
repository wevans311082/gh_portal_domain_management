from tests.agreement_helpers import attach_agreement
import pytest
from django.core.exceptions import ValidationError

from apps.domains.eu_registry import eu_contact_attributes, validate_eu_declaration
from apps.domains.models import ContactVerification
from tests.test_registration_submission import order
from tests.test_registration_submission import provider

pytestmark = pytest.mark.django_db


def declare(order, *, basis="resident", country="DE", citizenship="", kind="individual"):
    contact = order.registration_contact
    contact.registrant_type = kind
    contact.company = "Example Organization" if kind == "organisation" else ""
    contact.country = country
    contact.eu_eligibility_basis, contact.eu_citizenship_country, contact.eu_eligibility_certified = basis, citizenship, True
    return contact


@pytest.mark.parametrize("country", ["DE", "FR", "IE", "IS", "LI", "NO"])
def test_eligible_individual_residence_needs_no_business_or_citizenship(order, country):
    contact = declare(order, country=country)
    assert eu_contact_attributes(contact) == {}
    assert not contact.company_number and not contact.eu_citizenship_country


def test_eu_citizen_abroad_uses_explicit_citizenship_attribute(order):
    contact = declare(order, basis="citizen", country="GB", citizenship="IE")
    assert eu_contact_attributes(contact) == {"attr-name1": "countryOfCitizenship", "attr-value1": "IE"}
    payload = contact.as_resellerclub_payload("999", "eu")
    assert payload["type"] == "EuContact" and payload["company"] == "NA"
    assert payload["country"] == "GB" and payload["attr-value1"] == "IE"


@pytest.mark.parametrize("citizenship", ["IS", "LI", "NO"])
def test_eea_citizen_abroad_is_registry_eligible_but_provider_contract_needs_review(order, citizenship):
    contact = declare(order, basis="citizen", country="GB", citizenship=citizenship)
    validate_eu_declaration(contact)
    with pytest.raises(ValidationError, match="EURid accepts this citizenship"):
        eu_contact_attributes(contact)


@pytest.mark.parametrize("basis,kind", [("citizen", "organisation"), ("resident", "organisation"), ("established", "individual")])
def test_entity_basis_mismatch_cannot_be_submitted(order, basis, kind):
    contact = declare(order, basis=basis, kind=kind, citizenship="DE" if basis == "citizen" else "")
    with pytest.raises(ValidationError, match="registrant"):
        eu_contact_attributes(contact)


def test_organization_establishment_does_not_require_uk_company_number(order):
    assert eu_contact_attributes(declare(order, basis="established", kind="organisation")) == {}


def test_unneeded_citizenship_is_rejected_instead_of_collected(order):
    with pytest.raises(ValidationError, match="not needed"):
        eu_contact_attributes(declare(order, citizenship="DE"))


def test_non_reviewed_address_is_not_silently_replaced(order):
    contact = declare(order, country="GB")
    with pytest.raises(ValidationError, match="Do not substitute"):
        eu_contact_attributes(contact)
    assert contact.country == "GB"


def test_declaration_edits_invalidate_snapshot_and_blank_defaults_preserve_legacy_digest(order):
    original = order.registration_contact
    digest = ContactVerification.digest_for(original)
    contact = declare(order, country=original.country, basis="citizen", citizenship="DE")
    assert ContactVerification.digest_for(contact) != digest
    contact.eu_eligibility_basis, contact.eu_citizenship_country, contact.eu_eligibility_certified = "", "", False
    assert ContactVerification.digest_for(contact) == digest


def test_individual_form_saves_declared_citizenship_without_business_lookup():
    from unittest.mock import patch
    from apps.domains.forms import DomainContactForm
    from tests.test_registrant_type_form import data
    with patch("apps.domains.forms.CompaniesHouseService") as companies:
        form = DomainContactForm(data(eu_eligibility_basis="citizen", eu_citizenship_country="IE", eu_eligibility_certified="on"))
        assert form.is_valid(), form.errors
        contact = form.save(commit=False)
    companies.assert_not_called()
    assert contact.country == "GB" and contact.eu_citizenship_country == "IE"
    assert not contact.company_number and contact.eu_eligibility_certified


@pytest.mark.parametrize("changes,field", [({"eu_eligibility_basis": "citizen", "eu_citizenship_country": "IE"}, "eu_eligibility_certified"),
    ({"eu_eligibility_basis": "resident", "eu_citizenship_country": "IE", "eu_eligibility_certified": "on"}, "eu_citizenship_country"),
    ({"eu_eligibility_basis": "citizen", "eu_citizenship_country": "US", "eu_eligibility_certified": "on"}, "eu_citizenship_country")])
def test_incomplete_or_unneeded_citizenship_is_rejected_in_form(changes, field):
    from apps.domains.forms import DomainContactForm
    from tests.test_registrant_type_form import data
    form = DomainContactForm(data(**changes))
    assert not form.is_valid() and field in form.errors


def test_edit_form_does_not_preselect_certification(order):
    from apps.domains.forms import DomainContactForm
    contact = declare(order, basis="citizen", country="GB", citizenship="IE")
    assert DomainContactForm(instance=contact)["eu_eligibility_certified"].value() is False


def test_eu_preparation_uses_only_registrant_and_one_to_nine_nameservers(order, provider, monkeypatch):
    from apps.domains import registrar_policy
    from apps.domains.registration_submission import submit_registration
    from apps.domains.registry_requirements import validate_nameservers
    monkeypatch.setitem(registrar_policy.POLICIES, "eu", registrar_policy.EU)
    monkeypatch.setitem(registrar_policy.PROFILE_TLDS, "eu_presence", (registrar_policy.EU, ("eu",)))
    contact = declare(order, basis="citizen", country="GB", citizenship="IE")
    contact.save()
    order.domain_name, order.tld = "registrant-only.eu", "eu"
    order.save()
    attach_agreement(order)
    assert submit_registration(order.pk, "999", ["ns1.example.com"], client=provider) == "501"
    payload = provider.create_contact.call_args.args[0]
    assert payload["type"] == "EuContact" and payload["attr-value1"] == "IE"
    sent = provider.register_domain.call_args.kwargs
    assert sent["admin_contact_id"] == sent["tech_contact_id"] == sent["billing_contact_id"] == "-1"
    assert sent["purchase_privacy"] is False and sent["nameservers"] == ["ns1.example.com"]
    validate_nameservers(order.domain_name, [f"ns{i}.example.com" for i in range(9)])
    for hosts in ([], [f"ns{i}.example.com" for i in range(10)]):
        with pytest.raises(ValidationError, match="1 and 9"):
            validate_nameservers(order.domain_name, hosts)


def test_eu_single_configured_nameserver_is_used_without_whm_fallback(order, monkeypatch):
    from unittest.mock import patch
    from apps.domains import registrar_policy
    from apps.domains.tasks import _build_nameservers
    monkeypatch.setitem(registrar_policy.POLICIES, "eu", registrar_policy.EU)
    order.tld = "eu"
    with patch("apps.domains.tasks._configured_nameservers", return_value=["ns1.example.com"]), patch(
        "apps.provisioning.whm_client.WHMClient") as whm:
        assert _build_nameservers(order) == ["ns1.example.com"]
    whm.assert_not_called()


def test_configured_nameservers_are_not_silently_truncated(order, monkeypatch):
    from unittest.mock import patch
    from apps.domains import registrar_policy
    from apps.domains.tasks import _build_nameservers
    monkeypatch.setitem(registrar_policy.POLICIES, "eu", registrar_policy.EU)
    order.tld = "eu"
    nameservers = [f"ns{i}.example.com" for i in range(9)]
    with patch("apps.domains.tasks._configured_nameservers", return_value=nameservers):
        assert _build_nameservers(order) == nameservers


def test_historical_eu_single_server_order_can_be_matched(order, monkeypatch):
    from datetime import datetime, timezone
    from apps.domains import registrar_policy
    from apps.domains.historical_registration import verified_existing_order
    monkeypatch.setitem(registrar_policy.POLICIES, "eu", registrar_policy.EU)
    facts = verified_existing_order({"entityid": "501", "domainname": "historical.eu", "customerid": "999",
        "currentstatus": "Active", "registrantcontactid": "201", "noOfNameServers": "1", "ns1": "ns1.example.com",
        "creationtime": str(int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())),
        "endtime": str(int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()))},
        identifier="501", domain_name="historical.eu", customer_id="999")
    assert facts["nameservers"] == ["ns1.example.com"] and facts["role_ids"]["admin"] == "-1"


@pytest.mark.parametrize("name", ["a.eu", "ab--cd.eu", "de.eu", "registry.eu", "eurid.eu"])
def test_eu_invalid_or_reserved_name_stops_before_contact_creation(order, provider, monkeypatch, name):
    from apps.domains import registrar_policy
    from apps.domains.registration_submission import submit_registration
    monkeypatch.setitem(registrar_policy.POLICIES, "eu", registrar_policy.EU)
    declare(order, basis="citizen", country="GB", citizenship="IE").save()
    order.domain_name, order.tld = name, "eu"
    order.save()
    with pytest.raises(ValidationError):
        submit_registration(order.pk, "999", ["ns1.example.com"], client=provider)
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()


def test_eu_contact_email_cannot_depend_on_new_domain(order, provider, monkeypatch):
    from apps.domains import registrar_policy
    from apps.domains.registration_submission import submit_registration
    monkeypatch.setitem(registrar_policy.POLICIES, "eu", registrar_policy.EU)
    contact = declare(order, basis="citizen", country="GB", citizenship="IE")
    contact.email = "admin@brand-new.eu"
    contact.save()
    order.domain_name, order.tld = "brand-new.eu", "eu"
    order.save()
    with pytest.raises(ValidationError, match="reachable contact email"):
        submit_registration(order.pk, "999", ["ns1.example.com"], client=provider)
    provider.create_contact.assert_not_called()


def test_eu_renewal_uses_current_eligibility_and_rejects_unsynchronized_edits(order, provider, monkeypatch):
    from apps.domains import registrar_policy
    from apps.domains.models import Domain
    from apps.domains.registration_submission import submit_registration
    from apps.domains.registry_requirements import validate_renewal_registrant
    monkeypatch.setitem(registrar_policy.POLICIES, "eu", registrar_policy.EU)
    monkeypatch.setitem(registrar_policy.PROFILE_TLDS, "eu_presence", (registrar_policy.EU, ("eu",)))
    contact = declare(order, basis="citizen", country="GB", citizenship="IE")
    contact.save()
    order.domain_name, order.tld = "renewal-eligibility.eu", "eu"
    order.save()
    attach_agreement(order)
    submit_registration(order.pk, "999", ["ns1.example.com"], client=provider)
    order.refresh_from_db()
    domain = Domain.objects.create(user=order.user, name=order.domain_name, tld="eu", registrar_id="501", status="active")
    order.domain, order.status = domain, "completed"
    order.save()
    validate_renewal_registrant(domain)
    contact.eu_citizenship_country = "FR"
    contact.save()
    with pytest.raises(ValidationError, match="changed since registrar submission"):
        validate_renewal_registrant(domain)
