from unittest.mock import patch

import pytest

from apps.billing.models import Invoice
from apps.billing.services import convert_quote_to_invoice
from apps.domains.models import DomainContact, DomainOrder, TLDPricing
from apps.portal.cart_service import (
    add_domain_registration_item, create_invoice_from_cart, create_quote_from_cart, get_active_cart,
)
from apps.portal.models import PortalCartItem
from apps.portal.models import PortalCart


@pytest.fixture
def cart(db, django_user_model):
    user = django_user_model.objects.create_user(email="cart-check@example.com", password="testpass")
    contact = DomainContact.objects.create(
        user=user, registrant_type="individual", label="Home", name="Jane Smith", email=user.email,
        phone_country_code="44", phone="07700900000", address_line1="1 High Street",
        city="London", state="London", postcode="SW1A 1AA", country="GB", is_default=True,
    )
    TLDPricing.objects.create(tld="com", registration_cost="10", is_active=True)
    add_domain_registration_item(user=user, domain_name="cart-check.com", domain_contact_id=contact.pk)
    return get_active_cart(user)


@pytest.mark.parametrize("changes", [{"registrant_type": "unspecified"}, {"phone": "12"}, {"address_line1": ""}])
def test_invalid_contact_stops_before_invoice_or_email(cart, changes):
    DomainContact.objects.filter(user=cart.user).update(**changes)
    with patch("apps.portal.cart_service.create_invoice") as invoice:
        with pytest.raises(ValueError):
            create_invoice_from_cart(cart, send_email=True)
        invoice.assert_not_called()
    assert not Invoice.objects.exists()
    assert not DomainOrder.objects.exists()


def test_individual_can_checkout_without_company_or_registrar_email_approval(cart):
    with patch("apps.billing.services.email_document"):
        invoice = create_invoice_from_cart(cart)
    order = DomainOrder.objects.get(invoice=invoice)
    assert order.registration_contact.registrant_type == "individual"
    assert order.registration_contact.company_number == ""
    assert order.registration_contact.registrant_validation_status == "unvalidated"


def test_quote_acceptance_rechecks_contact_before_invoice_email(cart):
    quote = create_quote_from_cart(cart)
    DomainContact.objects.filter(user=cart.user).update(phone="12")
    with patch("apps.billing.services.create_invoice") as invoice:
        with pytest.raises(ValueError):
            convert_quote_to_invoice(quote, send_email=True)
        invoice.assert_not_called()
    quote.refresh_from_db()
    assert quote.converted_invoice_id is None
    assert not Invoice.objects.exists()
    assert not DomainOrder.objects.exists()


def test_foreign_contact_is_not_materialized(cart, django_user_model):
    other = django_user_model.objects.create_user(email="foreign-cart@example.com", password="testpass")
    foreign = DomainContact.objects.create(user=other, label="Foreign")
    PortalCartItem.objects.filter(cart=cart).update(domain_contact=foreign)
    with patch("apps.portal.cart_service.create_invoice") as invoice:
        with pytest.raises(ValueError):
            create_invoice_from_cart(cart)
        invoice.assert_not_called()
    assert not Invoice.objects.exists()


def test_explicit_unowned_selection_cannot_silently_use_default(cart, django_user_model):
    other = django_user_model.objects.create_user(email="unowned-cart@example.com", password="testpass")
    foreign = DomainContact.objects.create(user=other, label="Foreign")
    with pytest.raises(ValueError):
        add_domain_registration_item(user=cart.user, domain_name="different-cart.com", domain_contact_id=foreign.pk)
    assert cart.items.count() == 1


def test_unimplemented_tld_stops_before_invoice(cart):
    PortalCartItem.objects.filter(cart=cart).update(domain_name="cart-check.ca")
    with patch("apps.portal.cart_service.create_invoice") as invoice:
        with pytest.raises(ValueError):
            create_invoice_from_cart(cart)
        invoice.assert_not_called()


def test_uk_cart_order_does_not_request_unsupported_privacy(cart):
    PortalCartItem.objects.filter(cart=cart).update(domain_name="cart-check.co.uk", privacy_enabled=True)
    with patch("apps.billing.services.email_document"):
        invoice = create_invoice_from_cart(cart)
    assert not DomainOrder.objects.get(invoice=invoice).privacy_enabled


def test_repeated_checkout_with_stale_cart_returns_same_invoice(cart):
    with patch("apps.billing.services.email_document"):
        invoice = create_invoice_from_cart(cart)
        assert create_invoice_from_cart(cart).pk == invoice.pk
    assert Invoice.objects.count() == 1
    assert DomainOrder.objects.count() == 1


def test_quote_replay_does_not_allocate_a_second_quote(cart):
    quote = create_quote_from_cart(cart)
    assert create_quote_from_cart(cart).pk == quote.pk


def test_quote_cart_ambiguity_is_rejected_before_invoice_creation(cart):
    quote = create_quote_from_cart(cart)
    PortalCart.objects.create(user=cart.user, quote=quote, status="quoted")
    with patch("apps.billing.services.create_invoice") as invoice:
        with pytest.raises(ValueError, match="Multiple carts"):
            convert_quote_to_invoice(quote)
        invoice.assert_not_called()


def test_missing_contact_is_not_fabricated_during_checkout(cart):
    DomainContact.objects.filter(user=cart.user).delete()
    with patch("apps.portal.cart_service.create_invoice") as invoice:
        with pytest.raises(ValueError):
            create_invoice_from_cart(cart)
        invoice.assert_not_called()
    assert not DomainContact.objects.exists()


def test_quote_and_cart_owner_mismatch_is_rejected(cart, django_user_model):
    other = django_user_model.objects.create_user(email="quote-cart-other@example.com", password="testpass")
    quote = create_quote_from_cart(cart)
    quote.user = other
    quote.save()
    with patch("apps.billing.services.create_invoice") as invoice:
        with pytest.raises(ValueError, match="different accounts"):
            convert_quote_to_invoice(quote)
        invoice.assert_not_called()


def test_unsupported_transfer_stops_before_invoice(cart):
    PortalCartItem.objects.filter(cart=cart).update(item_type=PortalCartItem.TYPE_DOMAIN_TRANSFER, domain_name="cart-check.co.uk")
    with patch("apps.portal.cart_service.create_invoice") as invoice:
        with pytest.raises(ValueError, match="transfer workflow"):
            create_invoice_from_cart(cart)
        invoice.assert_not_called()


def test_renewal_owner_change_is_rechecked_before_invoice(cart, django_user_model):
    from apps.domains.models import Domain
    from apps.portal.cart_service import add_domain_renewal_item

    other = django_user_model.objects.create_user(email="renewal-new-owner@example.com", password="testpass")
    domain = Domain.objects.create(user=cart.user, name="renewal-cart.com", tld="com", status="active")
    add_domain_renewal_item(user=cart.user, domain_id=domain.pk)
    Domain.objects.filter(pk=domain.pk).update(user=other)
    with patch("apps.portal.cart_service.create_invoice") as invoice:
        with pytest.raises(ValueError, match="no longer belongs"):
            create_invoice_from_cart(cart)
        invoice.assert_not_called()


def test_public_acceptance_explains_contact_issue_without_partial_invoice(cart, client):
    from django.urls import reverse

    quote = create_quote_from_cart(cart)
    DomainContact.objects.filter(user=cart.user).update(registrant_type="unspecified")
    client.force_login(cart.user)
    with patch("apps.billing.services.email_document") as email:
        response = client.post(reverse("billing_public:quote_public_accept", args=[quote.public_token]))
        email.assert_not_called()
    assert response.status_code == 400
    assert b"Manage domain contacts" in response.content
    assert b"No invoice was created" in response.content
    quote.refresh_from_db()
    assert quote.converted_invoice_id is None
    assert not Invoice.objects.exists()
    assert not DomainOrder.objects.exists()


def test_late_domain_conflict_rolls_back_without_invoice_email(cart, django_capture_on_commit_callbacks):
    from apps.domains.models import Domain

    Domain.objects.create(user=cart.user, name="cart-check.com", tld="com")
    with patch("apps.billing.services.email_document") as email:
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            with pytest.raises(ValueError):
                create_invoice_from_cart(cart, send_email=True)
        email.assert_not_called()
    assert callbacks == []
    assert not Invoice.objects.exists()
    assert not DomainOrder.objects.exists()


def test_successful_checkout_emails_only_after_commit(cart, django_capture_on_commit_callbacks):
    with patch("apps.billing.services.email_document") as email:
        with django_capture_on_commit_callbacks(execute=True):
            invoice = create_invoice_from_cart(cart, send_email=True)
            email.assert_not_called()
            assert DomainOrder.objects.filter(invoice=invoice).exists()
        email.assert_called_once()
        assert email.call_args.args[0].pk == invoice.pk


def test_corrected_quote_can_be_accepted_after_preflight_failure(cart, client):
    from django.urls import reverse

    quote = create_quote_from_cart(cart)
    contact = DomainContact.objects.get(user=cart.user)
    contact.phone = "12"
    contact.save()
    client.force_login(cart.user)
    url = reverse("billing_public:quote_public_accept", args=[quote.public_token])
    assert client.post(url).status_code == 400
    contact.phone = "07700900000"
    contact.save()
    assert client.post(url).status_code == 302
    quote.refresh_from_db()
    assert quote.converted_invoice_id
    assert DomainOrder.objects.filter(invoice_id=quote.converted_invoice_id).count() == 1


def test_quote_review_identifies_owned_contact_and_escapes_its_label(cart, client):
    from django.urls import reverse

    quote = create_quote_from_cart(cart)
    DomainContact.objects.filter(user=cart.user).update(phone="12", label='<img src=x onerror="alert(1)">')
    client.force_login(cart.user)
    response = client.post(reverse("billing_public:quote_public_accept", args=[quote.public_token]))
    assert response.status_code == 400
    assert b"Phone:" in response.content
    assert b"cart-check.com" in response.content
    assert b"&lt;img" in response.content
    assert b'<img src=x onerror="alert(1)">' not in response.content


def test_default_contact_does_not_substitute_account_email_for_missing_name(cart):
    from apps.domains.services import DomainContactService

    assert not cart.user.first_name and not cart.user.last_name
    defaults = DomainContactService().build_default_contact(cart.user)
    assert defaults["name"] == ""
