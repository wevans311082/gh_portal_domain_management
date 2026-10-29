from tests.agreement_helpers import accept_cart_item
from tests.agreement_helpers import attach_agreement
from dataclasses import replace
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError

from apps.billing.models import Invoice
from apps.domains.models import DomainTransfer, DomainTransferAttempt, TLDPricing
from apps.domains.registrar_policy import POLICIES
from apps.domains.resellerclub_client import ResellerClubClient
from apps.domains.transfer_submission import submit_transfer
from apps.portal.cart_service import add_domain_transfer_item, create_invoice_from_cart, CartPreflightError
from apps.portal.models import PortalCartItem
from tests.test_cart_contact_preflight import cart
from tests.test_transfer_submission import transfer, provider, NS

pytestmark = pytest.mark.django_db


def test_four_contact_roles_do_not_implicitly_enable_a_transfer(transfer, provider, monkeypatch):
    monkeypatch.setitem(POLICIES, "com", replace(POLICIES["com"], transfer_workstream="review"))
    with pytest.raises(ValidationError, match="registrar-specific"):
        submit_transfer(transfer.pk, "999", NS, client=provider)
    provider.create_contact.assert_not_called()
    provider.transfer_domain.assert_not_called()
    assert not DomainTransferAttempt.objects.exists()


def test_acknowledged_transfer_is_not_resubmitted_after_profile_changes(transfer, provider, monkeypatch):
    attach_agreement(transfer)
    assert submit_transfer(transfer.pk, "999", NS, client=provider) == "501"
    monkeypatch.setitem(POLICIES, "com", replace(POLICIES["com"], transfer_workstream="review"))
    assert submit_transfer(transfer.pk, "999", NS, client=provider) == "501"
    assert provider.transfer_domain.call_count == provider.create_contact.call_count == 1


def test_policy_change_during_contact_preparation_stops_before_the_paid_marker(transfer, provider, monkeypatch):
    def change_policy(payload):
        monkeypatch.setitem(POLICIES, "com", replace(POLICIES["com"], transfer_auth_max=254))
        return "201"
    provider.create_contact.side_effect = change_policy
    attach_agreement(transfer)
    with pytest.raises(ValidationError, match="changed during preparation"):
        submit_transfer(transfer.pk, "999", NS, client=provider)
    provider.transfer_domain.assert_not_called()
    attempt = DomainTransferAttempt.objects.get()
    assert attempt.status == "aborted" and attempt.submitted_at is None


@pytest.mark.parametrize("nameservers", [[], ["ns1.example.com"], ["ns1.example.com", "NS1.EXAMPLE.COM."],
    [f"ns{i}.example.com" for i in range(14)]])
def test_transfer_nameserver_validation_precedes_contact_creation(transfer, provider, nameservers):
    with pytest.raises(ValidationError):
        submit_transfer(transfer.pk, "999", nameservers, client=provider)
    provider.create_contact.assert_not_called()
    provider.transfer_domain.assert_not_called()


@pytest.mark.parametrize("code", ["PRIVATE\x00CODE", "PRIVATE\x7fCODE", "X" * 256, None])
def test_malformed_secret_is_not_added_to_cart_or_echoed_in_errors(cart, code):
    before = cart.items.count()
    with pytest.raises(CartPreflightError) as error:
        add_domain_transfer_item(user=cart.user, domain_name="new-transfer.com", auth_code=code)
    if isinstance(code, str):
        assert code not in str(error.value)
    assert cart.items.count() == before and not Invoice.objects.exists()


def test_transfer_secret_is_preserved_without_silent_whitespace_changes(cart):
    code = " meaningful secret spaces "
    item = add_domain_transfer_item(user=cart.user, domain_name="new-transfer.com", auth_code=code)
    item.refresh_from_db()
    assert item.transfer_auth_code == code


def test_edited_cart_secret_is_rechecked_before_invoice_creation(cart):
    item = add_domain_transfer_item(user=cart.user, domain_name="new-transfer.com", auth_code="SAFE-CODE")
    PortalCartItem.objects.filter(pk=item.pk).update(transfer_auth_code="PRIVATE\x00CODE")
    with pytest.raises(CartPreflightError) as error:
        create_invoice_from_cart(cart)
    assert "PRIVATE" not in str(error.value)
    assert not Invoice.objects.exists()


def test_transfer_profile_is_rechecked_before_cart_invoice(cart, monkeypatch):
    add_domain_transfer_item(user=cart.user, domain_name="new-transfer.com", auth_code="SAFE-CODE")
    for item in cart.items.all():
        accept_cart_item(item)
    monkeypatch.setitem(POLICIES, "com", replace(POLICIES["com"], transfer_workstream="review"))
    with pytest.raises(CartPreflightError, match="registrar-specific"):
        create_invoice_from_cart(cart)
    assert not Invoice.objects.exists()


def test_required_at_submission_profile_cannot_accept_a_blank_code(transfer, provider, monkeypatch):
    monkeypatch.setitem(POLICIES, "com", replace(POLICIES["com"], transfer_auth_optional=False))
    transfer.auth_code = ""
    transfer.save()
    with pytest.raises(ValidationError, match="required.*before transfer submission"):
        submit_transfer(transfer.pk, "999", NS, client=provider)
    provider.create_contact.assert_not_called()
    provider.transfer_domain.assert_not_called()


@pytest.mark.parametrize("code,nameservers", [("PRIVATE\x00CODE", NS), ("X" * 256, NS), ("SAFE-CODE", NS[:1])])
def test_direct_transfer_client_cannot_bypass_shared_validation(code, nameservers):
    client = object.__new__(ResellerClubClient)
    client._post = Mock()
    with pytest.raises(ValidationError):
        client.transfer_domain("example.com", "999", "1", "2", "3", "4", nameservers, auth_code=code)
    client._post.assert_not_called()


@pytest.mark.parametrize("name", ["bad..com", "Mixed.com", "whitespace.com "])
def test_direct_transfer_client_rejects_malformed_or_unnormalized_domain_before_post(name):
    client = object.__new__(ResellerClubClient)
    client._post = Mock()
    with pytest.raises(ValidationError):
        client.transfer_domain(name, "999", "1", "2", "3", "4", NS, auth_code="SAFE-CODE")
    client._post.assert_not_called()


def test_us_authorization_code_can_be_deferred_using_the_documented_approval_flow(cart):
    contact = cart.user.domain_contacts.get()
    contact.us_nexus_category, contact.us_application_purpose, contact.us_nexus_certified = "C11", "P3", True
    contact.save()
    TLDPricing.objects.create(tld="us", transfer_cost=12, is_active=True)
    add_domain_transfer_item(user=cart.user, domain_name="deferred-transfer.us", auth_code="", domain_contact_id=contact.pk)
    cart.refresh_from_db()
    for cart_item in cart.items.all():
        accept_cart_item(cart_item)
    invoice = create_invoice_from_cart(cart, send_email=False)
    assert DomainTransfer.objects.get(invoice=invoice).auth_code == ""
    client = object.__new__(ResellerClubClient)
    client._post = Mock(return_value={"entityid": "501"})
    client.transfer_domain("deferred-transfer.us", "999", "1", "2", "3", "4", NS)
    assert "auth-code" not in client._post.call_args.args[1]
