from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Invoice
from apps.domains.models import Domain, DomainContact, DomainOrder, DomainRenewal, TLDPricing
from apps.domains.registry_requirements import validate_registration, validate_nameservers, validate_renewal_registrant, capabilities
from apps.domains.registration_submission import submit_registration
from apps.domains.tasks import execute_domain_renewal, process_auto_renewals
from apps.portal.cart_service import add_domain_registration_item, add_domain_renewal_item, create_invoice_from_cart, CartPreflightError
from tests.test_registration_submission import order, provider, NS

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("extension", ["uk", "co.uk", "org.uk", "me.uk"])
def test_individual_uk_registration_needs_no_company_number_and_uses_only_registrant(order, provider, extension):
    order.domain_name, order.tld = "personal." + extension, extension
    order.save()
    assert submit_registration(order.pk, "999", NS, client=provider) == "501"
    args = provider.register_domain.call_args.kwargs
    assert args["reg_contact_id"] == "201"
    assert args["admin_contact_id"] == args["tech_contact_id"] == args["billing_contact_id"] == "-1"
    assert args["purchase_privacy"] is False
    assert provider.create_contact.call_args.args[0]["type"] == "UkContact"
    assert not order.registration_contact.company_number


def test_me_uk_organization_is_refused_before_remote_contact_creation(order, provider):
    contact = order.registration_contact
    contact.registrant_type, contact.company = "organisation", "Example Company"
    contact.save()
    order.domain_name, order.tld = "business.me.uk", "me.uk"
    order.save()
    with pytest.raises(ValidationError, match="personal namespace"):
        submit_registration(order.pk, "999", NS, client=provider)
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()


@pytest.mark.parametrize("kind,field,value", [("individual", "name", "ABC"), ("individual", "name", "12345"),
    ("individual", "name", "A123"), ("organisation", "company", "12 AB")])
def test_uk_actual_holder_name_requires_four_characters_and_three_letters(order, kind, field, value):
    contact = order.registration_contact
    contact.registrant_type = kind
    if kind == "organisation":
        contact.company = "Example Company"
    setattr(contact, field, value)
    with pytest.raises(ValidationError, match="at least four characters"):
        contact.as_resellerclub_payload("999", "co.uk")


def test_short_actual_names_are_not_globally_padded_or_prohibited(order):
    contact = order.registration_contact
    contact.name = "Li"
    assert contact.as_resellerclub_payload("999", "com")["name"] == "Li"
    with pytest.raises(ValidationError):
        contact.as_resellerclub_payload("999", "uk")


def test_numeric_only_street_is_rejected_for_uk_registry_contact(order):
    contact = order.registration_contact
    contact.address_line1 = "123 45"
    with pytest.raises(ValidationError, match="non-numeric street information"):
        contact.as_resellerclub_payload("999", "uk")
    contact.address_line1 = "1-2-3"
    assert contact.as_resellerclub_payload("999", "uk")["address-line-1"] == "1-2-3"


@pytest.mark.parametrize("extension", ["co.uk", "org.uk", "uk"])
def test_other_uk_namespaces_do_not_require_a_company_or_reject_organizations(order, extension):
    validate_registration("person." + extension, order.registration_contact)
    contact = order.registration_contact
    contact.registrant_type, contact.company = "organisation", "Example Company"
    validate_registration("organization." + extension, contact)


@pytest.mark.parametrize("name", ["xn--bcher-kva.co.uk", "ab--cd.uk", "nested.example.co.uk", "xn--bcher-kva.com"])
def test_unsupported_idn_or_nested_registration_fails_before_provider_mutation(name):
    with pytest.raises(ValidationError):
        validate_registration(name)


@pytest.mark.parametrize("years", [0, 11, True, "1", None])
def test_registration_term_is_typed_and_bounded(years):
    with pytest.raises(ValidationError):
        validate_registration("example.co.uk", years=years)


@pytest.mark.parametrize("name", ["co.co.uk", "nhs.org.uk", "com.me.uk", "com.uk", "uk.uk"])
def test_reserved_uk_labels_cannot_reach_registration(name):
    with pytest.raises(ValidationError, match="reserved"):
        validate_registration(name)


def test_long_uk_name_has_provider_review_gate_without_changing_existing_domain_operations():
    name = "x" * 59 + ".co.uk"
    with pytest.raises(ValidationError, match="64-character total limit"):
        validate_registration(name)
    assert capabilities(name)["max_nameservers"] == 10
    validate_registration("x" * 63 + ".com")


def test_short_letters_remain_subject_to_provider_availability_not_an_invented_blanket_registry_ban():
    validate_registration("ab.co.uk")


def test_nameserver_limits_are_explicit_and_case_insensitive_duplicates_are_refused():
    ten = [f"ns{index}.example.com" for index in range(10)]
    validate_nameservers("example.uk", ten)
    with pytest.raises(ValidationError, match="2 and 10"):
        validate_nameservers("example.uk", ten + ["extra.example.com"])
    validate_nameservers("example.com", ten + [f"extra{index}.example.com" for index in range(3)])
    with pytest.raises(ValidationError, match="distinct"):
        validate_nameservers("example.uk", ["NS1.EXAMPLE.COM", "ns1.example.com."])


def test_registry_policy_rejects_organization_before_cart_invoice(order):
    contact = order.registration_contact
    contact.registrant_type, contact.company = "organisation", "Example Organization"
    contact.save()
    TLDPricing.objects.create(tld="me.uk", registration_cost=10)
    item = add_domain_registration_item(user=order.user, domain_name="business.me.uk", domain_contact_id=contact.pk)
    with pytest.raises(CartPreflightError, match="personal namespace"):
        create_invoice_from_cart(item.cart)
    assert not Invoice.objects.exists()


@pytest.mark.parametrize("extension", ["uk", "co.uk", "org.uk", "me.uk"])
@pytest.mark.parametrize("operation", ["toggle_lock", "get_auth_code"])
def test_uk_unsupported_operations_do_not_construct_provider_or_disclose_cached_code(order, client, extension, operation):
    domain = Domain.objects.create(user=order.user, name="personal." + extension, tld="com", status="active",
        registrar_id="501", epp_code="PRIVATE-STALE-UK-EPP")
    client.force_login(order.user)
    with patch("apps.domains.views.ResellerClubClient") as construct:
        response = client.post(reverse("domains:" + operation, args=[domain.pk]))
    assert response.status_code == 409 and response["Cache-Control"] == "no-store"
    assert "PRIVATE-STALE-UK-EPP" not in response.content.decode()
    construct.assert_not_called()


def test_domain_page_uses_actual_suffix_and_suppresses_uk_lock_and_epp_controls(order, client):
    domain = Domain.objects.create(user=order.user, name="personal.co.uk", tld="com", status="active",
        registrar_id="501", epp_code="PRIVATE-UK-EPP", is_locked=True)
    client.force_login(order.user)
    with patch("apps.provisioning.whm_client.WHMClient") as whm:
        whm.return_value.get_nameservers.return_value = []
        response = client.get(reverse("domains:detail", args=[domain.pk]))
    html = response.content.decode()
    assert "PRIVATE-UK-EPP" not in html and "Transfer locked" not in html
    assert reverse("domains:get_auth_code", args=[domain.pk]) not in html
    assert reverse("domains:toggle_lock", args=[domain.pk]) not in html
    assert "without an EPP code" in html


def test_unknown_extension_has_no_inferred_sensitive_operation_capability():
    assert capabilities("example.unreviewed")["lock"] is False
    assert capabilities("example.unreviewed")["auth_code"] is False
    assert capabilities("example.com")["lock"] is True


@pytest.mark.parametrize("extension", ["ltd.uk", "plc.uk", "sch.uk", "gov.uk", "net.uk"])
def test_unimplemented_uk_namespace_cannot_be_misclassified_as_open_uk(extension):
    from apps.domains.registrar_policy import tld_for_domain, policy_for
    from apps.domains.views import _split_domain_name
    assert tld_for_domain("example." + extension) == extension
    assert _split_domain_name("example." + extension) == ("example", extension)
    with pytest.raises(ValidationError, match="eligibility review"):
        policy_for(extension)


def test_registry_namespaces_are_not_registrable_customer_labels():
    with pytest.raises(ValidationError, match="registry namespace"):
        validate_registration("co.uk")


def test_me_uk_existing_renewal_cart_is_rechecked_after_registrant_becomes_organization(order):
    domain = Domain.objects.create(user=order.user, name="personal.me.uk", tld="me.uk", status="active", registrar_id="501")
    order.domain, order.domain_name, order.tld, order.registrar_order_id = domain, domain.name, "me.uk", "501"
    order.save()
    TLDPricing.objects.create(tld="me.uk", renewal_cost=10)
    item = add_domain_renewal_item(user=order.user, domain_id=domain.pk)
    DomainContact.objects.filter(pk=order.registration_contact_id).update(registrant_type="organisation", company="Example Company")
    with pytest.raises(CartPreflightError, match="personal namespace"):
        create_invoice_from_cart(item.cart)
    assert not Invoice.objects.exists()


def test_uk_oversized_whm_nameserver_list_is_rejected_before_registrar_construction(order, client):
    domain = Domain.objects.create(user=order.user, name="personal.co.uk", tld="co.uk", status="active", registrar_id="501")
    client.force_login(order.user)
    with patch("apps.provisioning.whm_client.WHMClient") as whm, patch("apps.domains.views.ResellerClubClient") as registrar:
        whm.return_value.get_nameservers.return_value = [f"ns{index}.example.com" for index in range(11)]
        response = client.post(reverse("domains:update_nameservers", args=[domain.pk]), {"action": "use_whm"})
    assert response.status_code == 302
    registrar.assert_not_called()


@pytest.mark.parametrize("extension", ["co.uk", "me.uk", "io"])
def test_country_code_summary_does_not_claim_a_missing_icann_raa_check(order, extension):
    from apps.domains.verification import registrar_email_summary
    domain = Domain.objects.create(user=order.user, name="personal." + extension, tld=extension)
    result = registrar_email_summary(domain)
    assert result["status"] == "not_applicable" and not result["can_refresh"] and not result["can_resend"]
    assert result["action_due_at"] is None


def test_me_uk_renewal_without_confirmed_registrant_is_reviewed_before_invoice_and_worker(order, provider, client):
    domain = Domain.objects.create(user=order.user, name="legacy.me.uk", tld="me.uk", status="active", registrar_id="501",
        auto_renew=True, expires_at=timezone.localdate() + timezone.timedelta(days=2))
    TLDPricing.objects.create(tld="me.uk", renewal_cost=10)
    with pytest.raises(ValidationError, match="registrant association needs review"):
        validate_renewal_registrant(domain)
    with pytest.raises(CartPreflightError, match="registrant association needs review"):
        add_domain_renewal_item(user=order.user, domain_id=domain.pk)
    client.force_login(order.user)
    assert client.post(reverse("domains:renew", args=[domain.pk])).status_code == 409
    assert process_auto_renewals.run() == 0 and not Invoice.objects.exists()
    with patch("apps.provisioning.whm_client.WHMClient") as whm:
        whm.return_value.get_nameservers.return_value = []
        detail = client.get(reverse("domains:detail", args=[domain.pk])).content.decode()
    assert "Renewal needs registrant review" in detail and "Auto-renew needs review" in detail
    assert "Renew wizard</a>" not in detail
    listing = client.get(reverse("domains:my_domains")).content.decode()
    assert "Renewal needs registrant review" in listing
    assert f'value="{domain.pk}" aria-label="Select {domain.name} for renewal"' not in listing
    assert client.post(reverse("domains:toggle_autorenew", args=[domain.pk])).status_code == 302
    assert client.post(reverse("domains:toggle_autorenew", args=[domain.pk])).status_code == 409
    domain.refresh_from_db()
    assert domain.auto_renew is False
    invoice = Invoice.objects.create(user=order.user, number="ME-REVIEW", status="paid", total=10)
    renewal = DomainRenewal.objects.create(user=order.user, domain=domain, invoice=invoice, status="paid")
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider) as construct:
        execute_domain_renewal.run(renewal.pk)
    construct.assert_not_called()
    renewal.refresh_from_db()
    assert renewal.status == "failed" and "needs review" in renewal.last_error
