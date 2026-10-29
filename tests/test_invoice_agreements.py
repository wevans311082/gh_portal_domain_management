import pytest
from unittest.mock import patch

from django.urls import reverse

from apps.billing.models import Invoice, Quote
from apps.billing.services import convert_quote_to_invoice
from apps.domains.agreements import publish_agreement
from apps.domains.models import DomainAgreementAcceptance, DomainOrder, DomainTransfer
from apps.portal.cart_service import create_invoice_from_cart, create_quote_from_cart, CartPreflightError
from apps.portal.models import PortalCart, PortalCartItem
from tests.agreement_helpers import accept_cart_item, publish_test_agreement
from tests.test_cart_contact_preflight import cart
from tests.test_registrar_agreements import owner, publication

pytestmark = pytest.mark.django_db


@pytest.fixture(params=["domain_registration", "domain_transfer"])
def item(request, cart):
    item = cart.items.get()
    item.item_type = request.param
    item.save(update_fields=["item_type"])
    return item


def test_missing_acceptance_cannot_allocate_invoice_or_partial_request(item):
    publish_test_agreement()
    with patch("apps.portal.cart_service.create_invoice") as allocate, pytest.raises(CartPreflightError) as error:
        create_invoice_from_cart(item.cart)
    allocate.assert_not_called()
    assert error.value.agreement_item_id == item.pk
    assert not Invoice.objects.exists() and not DomainOrder.objects.exists() and not DomainTransfer.objects.exists()


def test_accepted_item_materializes_the_exact_customer_receipt(item):
    receipt = accept_cart_item(item)
    invoice = create_invoice_from_cart(item.cart, send_email=False)
    request_model = DomainOrder if item.item_type == "domain_registration" else DomainTransfer
    request = request_model.objects.get(invoice=invoice)
    assert request.agreement_acceptance_id == receipt.pk
    assert request.user_id == receipt.customer_id and request.registration_contact_id == receipt.contact_id


@pytest.mark.parametrize("change", ["contact", "terms"])
def test_changed_contact_or_terms_stop_invoice_before_allocation(item, owner, change):
    accept_cart_item(item)
    if change == "contact":
        contact = item.domain_contact
        contact.name = "Updated Registrant"
        contact.save(update_fields=["name"])
    else:
        publish_agreement(actor=owner, **publication(version="invoice-new-terms"))
    with patch("apps.portal.cart_service.create_invoice") as allocate, pytest.raises(CartPreflightError):
        create_invoice_from_cart(item.cart)
    allocate.assert_not_called()
    assert not Invoice.objects.exists()


def test_quotes_can_be_issued_but_not_converted_without_customer_acceptance(item):
    quote = create_quote_from_cart(item.cart)
    assert not DomainAgreementAcceptance.objects.exists()
    with patch("apps.billing.services.create_invoice") as allocate, pytest.raises(CartPreflightError):
        convert_quote_to_invoice(quote)
    allocate.assert_not_called()
    quote.refresh_from_db()
    assert quote.status == Quote.STATUS_SENT and quote.converted_invoice_id is None
    assert not Invoice.objects.exists()


def test_public_quote_hold_has_scoped_review_link_and_rolls_back_acceptance(item, client):
    publish_test_agreement()
    quote = create_quote_from_cart(item.cart)
    client.force_login(item.cart.user)
    with patch("apps.billing.services.email_document") as email:
        response = client.post(reverse("billing_public:quote_public_accept", args=[quote.public_token]))
    email.assert_not_called()
    assert response.status_code == 400
    assert reverse("domains:cart_agreements", args=[item.pk]).encode() in response.content
    quote.refresh_from_db()
    assert quote.status == Quote.STATUS_SENT and quote.accepted_at is None and quote.converted_invoice_id is None
    assert not Invoice.objects.exists() and not DomainOrder.objects.exists() and not DomainTransfer.objects.exists()
    accept_cart_item(item)
    response = client.post(reverse("billing_public:quote_public_accept", args=[quote.public_token]))
    assert response.status_code == 302
    quote.refresh_from_db()
    assert quote.converted_invoice_id


def test_existing_invoice_replay_does_not_create_or_rewrite_after_new_terms(item, owner):
    receipt = accept_cart_item(item)
    first = create_invoice_from_cart(item.cart, send_email=False)
    publish_agreement(actor=owner, **publication(version="after-existing-invoice"))
    assert create_invoice_from_cart(item.cart, send_email=False).pk == first.pk
    assert Invoice.objects.count() == 1
    model = DomainOrder if item.item_type == "domain_registration" else DomainTransfer
    assert model.objects.get(invoice=first).agreement_acceptance_id == receipt.pk


def test_staff_created_cart_is_discoverable_by_its_customer_only(cart, client, owner, django_user_model):
    PortalCart.objects.filter(pk=cart.pk).update(created_by_staff=owner)
    client.force_login(cart.user)
    response = client.get(reverse("domains:agreement_requests"))
    assert b"cart-check.com" in response.content
    assert reverse("domains:request_agreements", kwargs={"kind": "cart", "pk": cart.items.get().pk}).encode() in response.content
    other = django_user_model.objects.create_user(email="other-invoice-agreement@example.test", password="testpass")
    client.force_login(other)
    assert b"cart-check.com" not in client.get(reverse("domains:agreement_requests")).content


def test_manual_invoice_cannot_bypass_review_for_unsubmitted_request(client, owner, django_user_model):
    from tests.security_helpers import login_owner
    from tests.test_registration_submission import _new_order
    request = _new_order(django_user_model)
    login_owner(client, owner)
    response = client.post(reverse("admin_tools:manual_domain_generate_invoice", args=[request.pk]), {"amount": "12"})
    assert response.status_code == 302 and not Invoice.objects.exists()
    request.refresh_from_db()
    assert request.invoice_id is None


@pytest.mark.parametrize("problem", ["cancelled", "foreign_role"])
def test_manual_invoice_rechecks_request_state_and_owned_roles(client, owner, django_user_model, problem):
    from tests.security_helpers import login_owner
    from tests.test_registration_submission import _new_order
    from tests.agreement_helpers import attach_agreement
    from apps.domains.models import DomainContact
    request = _new_order(django_user_model)
    attach_agreement(request)
    if problem == "cancelled":
        request.status = "cancelled"
        request.save(update_fields=["status"])
    else:
        other = django_user_model.objects.create_user(email="foreign-invoice-role@example.test", password="testpass")
        request.billing_contact = DomainContact.objects.create(user=other, label="Foreign")
        request.save(update_fields=["billing_contact"])
    login_owner(client, owner)
    assert client.post(reverse("admin_tools:manual_domain_generate_invoice", args=[request.pk])).status_code == 302
    assert not Invoice.objects.exists()
