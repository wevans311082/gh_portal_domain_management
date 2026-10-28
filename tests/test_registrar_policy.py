from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError

from apps.domains.contact_validation import validate_order_contacts
from apps.domains.forms import DomainRegistrationForm
from apps.domains.models import DomainContact, DomainOrder
from apps.domains.registrar_policy import ALL_ROLES, policy_for, tld_for_domain
from apps.domains.resellerclub_client import ResellerClubClient


def test_shared_profile_membership_and_independent_email_workstream():
    assert policy_for("com") is policy_for("net") is policy_for("org")
    assert policy_for("uk") is policy_for("co.uk") is policy_for("me.uk")
    assert policy_for("com").email_workstream == "icann_raa"
    assert policy_for("io").email_workstream == "registry_specific"
    assert policy_for("us").eligibility_workstream == "us_nexus"


def test_duplicate_tld_assignment_cannot_silently_replace_a_profile():
    from apps.domains.registrar_policy import GENERAL, US, build_policy_catalogue
    with pytest.raises(ValueError, match="unique"):
        build_policy_catalogue({"first": (GENERAL, ("com",)), "second": (US, ("com",))})


def test_service_roles_use_profile_routing_and_normalize_extension():
    from apps.domains.registrar_policy import contact_payload_tld
    assert contact_payload_tld(".US", "registration_contact") == "us"
    assert contact_payload_tld(".US", "admin_contact") == "com"
    assert contact_payload_tld("co.uk", "registration_contact") == "co.uk"
    with pytest.raises(ValidationError):
        contact_payload_tld("unknown", "registration_contact")


@pytest.mark.parametrize("extension", ["com", "net", "org", "io"])
def test_general_contact_routing(extension):
    assert policy_for(extension).roles == ALL_ROLES
    assert policy_for(extension).contact_type == "Contact"


@pytest.mark.parametrize("extension", ["uk", "co.uk", "org.uk", "me.uk"])
def test_uk_contact_routing(extension):
    policy = policy_for(extension)
    assert policy.roles == ("registration_contact",)
    assert not policy.privacy_supported
    assert tld_for_domain("Example." + extension) == extension


@pytest.mark.parametrize("extension", ["ca", "eu", "es", "de", "unknown", "com.invalid", ""])
def test_unimplemented_eligibility_requires_review(extension):
    with pytest.raises(ValidationError, match="review"):
        policy_for(extension)


def client_without_network():
    client = object.__new__(ResellerClubClient)
    client._post = Mock(return_value={"entityid": "123"})
    return client


@pytest.mark.parametrize("extension", ["uk", "co.uk", "org.uk", "me.uk", "com", "io"])
def test_actual_api_registration_request_obeys_contact_policy(extension):
    client = client_without_network()
    client.register_domain(
        "example." + extension, 1, "999", "1", "2", "3", "4",
        ["ns1.example.com", "ns2.example.com"], purchase_privacy=True,
    )
    endpoint, payload = client._post.call_args.args
    assert endpoint == "domains/register"
    assert payload["invoice-option"] == "NoInvoice"
    if extension.endswith("uk"):
        assert payload["purchase-privacy"] is False
        assert [payload[field] for field in ("reg-contact-id", "admin-contact-id", "tech-contact-id", "billing-contact-id")] == ["1", "-1", "-1", "-1"]
    else:
        assert payload["purchase-privacy"] is True
        assert payload["admin-contact-id"] == "2"


def test_direct_api_call_cannot_bypass_review_gate():
    client = client_without_network()
    with pytest.raises(ValidationError):
        client.register_domain("example.ca", 1, "999", "1", "2", "3", "4", [])
    client._post.assert_not_called()


def test_contact_service_cannot_submit_unreviewed_tld(registrant):
    from apps.domains.services import DomainContactService

    provider = Mock()
    with pytest.raises(ValidationError, match="review"):
        DomainContactService(client=provider).sync_remote_contact(registrant, "999", "es")
    provider.create_contact.assert_not_called()
    provider.update_contact.assert_not_called()


@pytest.mark.parametrize("domain", ["example.co.uk", "example.ca"])
def test_direct_transfer_cannot_bypass_unimplemented_workflow(domain):
    client = client_without_network()
    with pytest.raises(ValidationError):
        client.transfer_domain(domain, "999", "1", "2", "3", "4", [])
    client._post.assert_not_called()


@pytest.fixture
def registrant(db, django_user_model):
    user = django_user_model.objects.create_user(email="policy@example.com", password="testpass")
    return DomainContact.objects.create(
        user=user, registrant_type="individual", label="Home", name="Jane Smith",
        email=user.email, phone_country_code="44", phone="07700900000",
        address_line1="1 High Street", city="London", state="London",
        postcode="SW1A 1AA", country="GB",
    )


def test_registration_form_explains_unsupported_privacy_and_ignores_tampering(registrant):
    form = DomainRegistrationForm({
        "domain_name": "example.co.uk", "registration_years": "1", "contact": registrant.pk,
        "dns_provider": "cpanel", "privacy_enabled": "on",
    }, user=registrant.user)
    assert form.is_valid(), form.errors
    assert form.fields["privacy_enabled"].disabled
    assert "not supported" in form.fields["privacy_enabled"].help_text
    assert form.cleaned_data["privacy_enabled"] is False


def test_registration_form_rejects_unimplemented_eligibility(registrant):
    form = DomainRegistrationForm({
        "domain_name": "example.ca", "registration_years": "1", "contact": registrant.pk,
        "dns_provider": "cpanel",
    }, user=registrant.user)
    assert not form.is_valid()
    assert "domain_name" in form.errors


def test_checkout_review_stops_before_invoice_creation(registrant, client):
    from django.urls import reverse
    from apps.domains.models import TLDPricing

    TLDPricing.objects.create(tld="ca", registration_cost="10", is_active=True)
    client.force_login(registrant.user)
    with patch("apps.domains.views.create_invoice") as invoice:
        response = client.post(reverse("domains:register"), {
            "domain_name": "example.ca", "registration_years": "1", "contact": registrant.pk,
            "dns_provider": "cpanel",
        })
    assert response.status_code == 200
    assert b"requires a registrar eligibility review" in response.content
    assert b"disabled" in response.content
    invoice.assert_not_called()
    assert not DomainOrder.objects.exists()


def test_stored_tld_cannot_disagree_with_actual_domain(registrant):
    order = SimpleNamespace(
        user_id=registrant.user_id, domain_name="example.com",
        registration_contact=registrant,
    )
    with pytest.raises(ValidationError, match="does not match"):
        validate_order_contacts(order, "999", "uk")


def test_uk_worker_does_not_validate_or_send_unused_contact_roles(registrant, settings):
    from apps.domains.tasks import register_domain_order

    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    incomplete = DomainContact.objects.create(user=registrant.user, label="Unused")
    order = DomainOrder.objects.create(
        user=registrant.user, domain_name="example.co.uk", tld="co.uk", status="paid",
        registration_contact=registrant, admin_contact=incomplete,
        tech_contact=incomplete, billing_contact=incomplete, privacy_enabled=True,
        dns_provider="registrar",
    )
    provider = Mock()
    provider.create_contact.return_value = {"contact_id": "123"}
    provider.register_domain.return_value = {"entityid": "456"}
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch(
        "apps.domains.tasks._build_nameservers", return_value=["ns1.example.com", "ns2.example.com"]
    ):
        register_domain_order.run(order.pk)
    provider.create_contact.assert_called_once()
    request = provider.register_domain.call_args.kwargs
    assert request["reg_contact_id"] == "123"
    assert request["admin_contact_id"] == request["tech_contact_id"] == request["billing_contact_id"] == "-1"
    assert request["purchase_privacy"] is False
    order.refresh_from_db()
    assert not order.privacy_enabled
    assert order.status == "completed"
