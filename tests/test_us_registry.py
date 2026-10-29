from tests.agreement_helpers import attach_agreement
import pytest
from django.core.exceptions import ValidationError

from apps.domains.models import ContactVerification
from apps.domains.us_registry import us_contact_attributes
from tests.test_registration_submission import order
from tests.test_registration_submission import provider, NS

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("category", ["C11", "C12"])
def test_individual_nexus_needs_no_company_or_us_address_inference(order, category):
    contact = order.registration_contact
    contact.us_nexus_category, contact.us_application_purpose, contact.us_nexus_certified = category, "P3", True
    result = us_contact_attributes(contact)
    assert result == {"attr-name1": "purpose", "attr-value1": "P3", "attr-name2": "category", "attr-value2": category}
    assert not contact.company_number and contact.country == "GB"


@pytest.mark.parametrize("category,kind", [("C11", "organisation"), ("C12", "organisation"), ("C21", "individual")])
def test_incompatible_entity_category_is_rejected(order, category, kind):
    contact = order.registration_contact
    contact.registrant_type = kind
    contact.us_nexus_category, contact.us_application_purpose, contact.us_nexus_certified = category, "P1", True
    with pytest.raises(ValidationError, match="registrant"):
        us_contact_attributes(contact)


@pytest.mark.parametrize("category", ["C31", "C32"])
def test_foreign_entity_requires_explicit_country_contract_review(order, category):
    contact = order.registration_contact
    contact.registrant_type = "organisation"
    contact.us_nexus_category, contact.us_application_purpose, contact.us_nexus_certified = category, "P1", True
    with pytest.raises(ValidationError, match="country declaration"):
        us_contact_attributes(contact)


def test_empty_declarations_are_never_inferred_from_country(order):
    contact = order.registration_contact
    contact.country = "US"
    with pytest.raises(ValidationError, match="not inferred"):
        us_contact_attributes(contact)


def test_nexus_edits_invalidate_evidence_without_changing_blank_legacy_hashes(order):
    contact = order.registration_contact
    initial = ContactVerification.digest_for(contact)
    contact.us_nexus_category, contact.us_application_purpose, contact.us_nexus_certified = "C11", "P3", True
    assert ContactVerification.digest_for(contact) != initial
    contact.us_nexus_category, contact.us_application_purpose, contact.us_nexus_certified = "", "", False
    assert ContactVerification.digest_for(contact) == initial


def test_contact_form_requires_explicit_certification_but_does_not_validate_business():
    from unittest.mock import patch
    from apps.domains.forms import DomainContactForm
    from tests.test_registrant_type_form import data
    with patch("apps.domains.forms.CompaniesHouseService") as companies:
        missing = DomainContactForm(data(us_nexus_category="C11", us_application_purpose="P3"))
        assert not missing.is_valid() and "us_nexus_certified" in missing.errors
        complete = DomainContactForm(data(us_nexus_category="C11", us_application_purpose="P3", us_nexus_certified="on"))
        assert complete.is_valid(), complete.errors
        assert complete.save(commit=False).us_nexus_certified is True
    companies.assert_not_called()


def test_service_contact_payload_does_not_require_or_leak_registrant_nexus(order):
    from apps.domains.registrar_policy import contact_payload_tld
    contact = order.registration_contact
    for role in ("admin_contact", "tech_contact", "billing_contact"):
        payload = contact.as_resellerclub_payload("999", contact_payload_tld("us", role))
        assert "attr-name1" not in payload
    with pytest.raises(ValidationError):
        contact.as_resellerclub_payload("999", contact_payload_tld("us", "registration_contact"))


def test_preparation_creates_distinct_nexus_and_service_bindings_for_same_contact(order, provider):
    from apps.domains.registration_submission import submit_registration
    # Exercise durable preparation under the actual reviewed contact policy.
    contact = order.registration_contact
    contact.us_nexus_category, contact.us_application_purpose, contact.us_nexus_certified = "C11", "P3", True
    contact.save()
    order.domain_name, order.tld, order.privacy_enabled = "prepared-person.us", "us", False
    order.save()
    provider.create_contact.side_effect = ["201", "202"]
    attach_agreement(order)
    assert submit_registration(order.pk, "999", NS, client=provider) == "501"
    first, second = [call.args[0] for call in provider.create_contact.call_args_list]
    assert first["attr-name2"] == "category" and first["attr-value2"] == "C11"
    assert "attr-name1" not in second
    sent = provider.register_domain.call_args.kwargs
    assert sent["reg_contact_id"] == "201" and sent["admin_contact_id"] == sent["tech_contact_id"] == sent["billing_contact_id"] == "202"


def test_us_cart_suppresses_privacy_and_rechecks_nexus_before_invoice(order):
    from apps.domains.models import DomainOrder, TLDPricing
    from apps.portal.cart_service import add_domain_registration_item, create_invoice_from_cart
    TLDPricing.objects.create(tld="us", registration_cost=12, is_active=True)
    contact = order.registration_contact
    item = add_domain_registration_item(user=order.user, domain_name="nexus-cart.us", domain_contact_id=contact.pk, privacy_enabled=True)
    assert item.privacy_enabled is False
    with pytest.raises(ValueError, match="nexus"):
        create_invoice_from_cart(item.cart, send_email=False)
    contact.us_nexus_category, contact.us_application_purpose, contact.us_nexus_certified = "C11", "P3", True
    contact.save()
    invoice = create_invoice_from_cart(item.cart, send_email=False)
    assert DomainOrder.objects.get(invoice=invoice).privacy_enabled is False


def test_us_renewal_requires_retained_current_nexus_snapshot(order, provider):
    from apps.domains.models import Domain
    from apps.domains.registration_submission import submit_registration
    from apps.domains.registry_requirements import validate_renewal_registrant
    contact = order.registration_contact
    contact.us_nexus_category, contact.us_application_purpose, contact.us_nexus_certified = "C11", "P3", True
    contact.save()
    order.domain_name, order.tld = "renewal-nexus.us", "us"
    order.save()
    provider.create_contact.side_effect = ["201", "202"]
    attach_agreement(order)
    submit_registration(order.pk, "999", NS, client=provider)
    order.refresh_from_db()
    domain = Domain.objects.create(user=order.user, name=order.domain_name, tld="us", registrar_id="501", status="active")
    order.domain, order.status = domain, "completed"
    order.save()
    validate_renewal_registrant(domain)
    contact.us_application_purpose = "P1"
    contact.save()
    with pytest.raises(ValidationError, match="changed since registrar submission"):
        validate_renewal_registrant(domain)


@pytest.mark.parametrize("code", ["abcde", "a" * 17, "abcdef ", "abc\ndef", "abc\u00a0def"])
def test_invalid_us_transfer_code_stops_before_provider(code):
    from unittest.mock import Mock
    from apps.domains.resellerclub_client import ResellerClubClient
    api = object.__new__(ResellerClubClient)
    api._post = Mock()
    with pytest.raises(ValidationError, match="6-16"):
        api.transfer_domain("transfer-nexus.us", "999", "201", "202", "202", "202", NS, auth_code=code)
    api._post.assert_not_called()


def test_us_is_not_displayed_as_generic_raa_workflow(order):
    from apps.domains.models import Domain
    from apps.domains.verification import registrar_email_summary
    domain = Domain(user=order.user, name="personal.us", tld="com")
    summary = registrar_email_summary(domain)
    assert summary["status"] == "not_applicable" and not summary["can_resend"] and summary["action_due_at"] is None
