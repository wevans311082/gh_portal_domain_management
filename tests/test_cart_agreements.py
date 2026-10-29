import uuid
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.domains.agreements import current_agreement_plan, publish_agreement
from apps.domains.cart_agreements import accept_cart_agreements, cart_agreement_target
from apps.domains.models import ContactVerification, DomainAgreementAcceptance
from apps.portal.models import PortalCart, PortalCartItem
from tests.test_registrar_agreements import document, publication, owner, order

pytestmark = pytest.mark.django_db


@pytest.fixture
def item(order):
    cart = PortalCart.objects.create(user=order.user)
    return PortalCartItem.objects.create(cart=cart, item_type="domain_registration", domain_name=order.domain_name,
        domain_contact=order.registration_contact, description="Test domain", unit_price="12.00")


def acceptance(item, **changes):
    target, contact, operation, digest = cart_agreement_target(item.cart.user, item.pk)
    values = dict(customer=item.cart.user, item_id=item.pk, context_digest=digest, key=uuid.uuid4(),
        subject_digest=ContactVerification.digest_for(contact),
        plan_digest=current_agreement_plan(item.domain_name, operation).digest, accepted=True)
    values.update(changes)
    return values


def url(item):
    return reverse("domains:cart_agreements", args=[item.pk])


def test_get_is_read_only_and_exact_text_is_escaped(client, item, document):
    client.force_login(item.cart.user)
    response = client.get(url(item))
    assert response.status_code == 200
    assert "&lt;script&gt;plain text&lt;/script&gt;" in response.content.decode()
    assert "checked" not in str(response.context["form"]["accepted"])
    assert not DomainAgreementAcceptance.objects.exists()
    assert "no-store" in response["Cache-Control"]


def test_customer_acceptance_attaches_exact_receipt_and_replay_is_idempotent(item, document):
    values = acceptance(item)
    receipt = accept_cart_agreements(**values)
    assert accept_cart_agreements(**values).pk == receipt.pk
    item.refresh_from_db()
    assert item.agreement_acceptance_id == receipt.pk
    assert receipt.documents.get().document_id == document.pk
    assert AuditLog.objects.filter(action="domain.agreement.attached").count() == 1


@pytest.mark.parametrize("field,value", [("domain_name", "changed.com"), ("unit_price", "99.00"),
    ("transfer_auth_code", "changed-secret"), ("auto_renew", False)])
def test_request_changes_reject_previously_displayed_review(item, document, field, value):
    values = acceptance(item)
    PortalCartItem.objects.filter(pk=item.pk).update(**{field: value})
    with pytest.raises(ValidationError, match="cart item changed"):
        accept_cart_agreements(**values)
    assert not DomainAgreementAcceptance.objects.exists()


def test_contact_changes_reject_review(item, document):
    values = acceptance(item)
    contact = item.domain_contact
    contact.name = "Changed person"
    contact.save()
    with pytest.raises(ValidationError, match="contact changed"):
        accept_cart_agreements(**values)
    assert not DomainAgreementAcceptance.objects.exists()


def test_old_replay_cannot_replace_current_terms(item, document, owner):
    old_values = acceptance(item)
    old = accept_cart_agreements(**old_values)
    publish_agreement(actor=owner, **publication(version="test-v2"))
    new = accept_cart_agreements(**acceptance(item))
    with pytest.raises(ValidationError, match="Current customer acceptance"):
        accept_cart_agreements(**old_values)
    item.refresh_from_db()
    assert item.agreement_acceptance_id == new.pk != old.pk


def test_attachment_audit_failure_rolls_back_receipt_and_attachment(item, document):
    values = acceptance(item)
    original = AuditLog.objects.create
    def audit(**kwargs):
        if kwargs["action"] == "domain.agreement.attached":
            raise DatabaseError("test failure")
        return original(**kwargs)
    with patch.object(AuditLog.objects, "create", side_effect=audit), pytest.raises(DatabaseError):
        accept_cart_agreements(**values)
    item.refresh_from_db()
    assert item.agreement_acceptance_id is None and not DomainAgreementAcceptance.objects.exists()


def test_customer_post_and_stale_terms_reset_checkbox(client, item, document, owner):
    client.force_login(item.cart.user)
    review = client.get(url(item)).context["form"].initial["review"]
    publish_agreement(actor=owner, **publication(version="test-v2"))
    response = client.post(url(item), {"review": review, "accepted": "on"})
    assert response.status_code == 400 and not DomainAgreementAcceptance.objects.exists()
    assert not response.context["form"].is_bound
    fresh_review = response.context["form"].initial["review"]
    assert client.post(url(item), {"review": fresh_review, "accepted": "on"}).status_code == 302


@pytest.mark.parametrize("session_key", ["owner_impersonation", "impersonator_user_id"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_impersonation_cannot_review_or_accept(client, item, document, session_key, method):
    client.force_login(item.cart.user)
    session = client.session
    session[session_key] = 123
    session.save()
    assert getattr(client, method)(url(item)).status_code == 403
    assert not DomainAgreementAcceptance.objects.exists()


def test_staff_and_foreign_customer_cannot_read(client, item, document, owner, django_user_model):
    client.force_login(owner)
    assert client.get(url(item)).status_code == 403
    other = django_user_model.objects.create_user(email="other-review@example.test", password="testpass")
    client.force_login(other)
    assert client.get(url(item)).status_code == 404


@pytest.mark.parametrize("status", ["invoiced", "abandoned"])
def test_closed_carts_do_not_accept(item, document, status):
    values = acceptance(item)
    PortalCart.objects.filter(pk=item.cart_id).update(status=status)
    with pytest.raises(ValidationError, match="submitted or closed"):
        accept_cart_agreements(**values)
    assert not DomainAgreementAcceptance.objects.exists()


def test_missing_terms_and_missing_explicit_contact_never_show_acceptance(client, item):
    client.force_login(item.cart.user)
    response = client.get(url(item))
    assert response.status_code == 200 and "form" not in response.context
    PortalCartItem.objects.filter(pk=item.pk).update(domain_contact=None)
    response = client.get(url(item))
    assert "removing and re-adding" in response.content.decode()
    assert not DomainAgreementAcceptance.objects.exists()


def test_csrf_and_duplicate_fields(client, item, document):
    from django.test import Client
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(item.cart.user)
    assert strict.post(url(item), {}).status_code == 403
    client.force_login(item.cart.user)
    review = client.get(url(item)).context["form"].initial["review"]
    response = client.post(url(item), {"review": [review, review], "accepted": "on"})
    assert response.status_code == 400 and not DomainAgreementAcceptance.objects.exists()


def test_valid_impersonation_context_is_rejected_by_view(item, document, owner):
    from django.test import RequestFactory
    from django.core.exceptions import PermissionDenied
    from apps.domains.agreement_views import cart_agreements
    request = RequestFactory().get(url(item))
    request.user = item.cart.user
    request.session = {"owner_impersonation": {"owner_id": owner.pk}}
    request.impersonation_actor = owner
    with pytest.raises(PermissionDenied):
        cart_agreements(request, item.pk)


@pytest.mark.parametrize("operation", ["registration", "transfer"])
def test_receipt_survives_cart_materialization(item, document, order, operation):
    from apps.domains.models import DomainOrder, DomainTransfer
    from apps.portal.cart_service import create_invoice_from_cart
    order.delete()
    if operation == "transfer":
        item.item_type = "domain_transfer"
        item.save(update_fields=["item_type"])
    receipt = accept_cart_agreements(**acceptance(item))
    invoice = create_invoice_from_cart(item.cart, send_email=False)
    model = DomainOrder if operation == "registration" else DomainTransfer
    assert model.objects.get(invoice=invoice).agreement_acceptance_id == receipt.pk


@pytest.mark.parametrize("status", ["sent", "viewed", "accepted"])
def test_issued_quote_can_still_receive_real_customer_agreement_acceptance(client, item, document, status):
    from apps.portal.cart_service import create_quote_from_cart
    quote = create_quote_from_cart(item.cart)
    quote.status = status
    quote.save(update_fields=["status"])
    item.cart.refresh_from_db()
    assert item.cart.submitted_at is not None
    client.force_login(item.cart.user)
    response = client.get(url(item))
    assert response.status_code == 200 and response.context["form"]
    assert reverse("billing_public:quote_public", args=[quote.public_token]).encode() in response.content
    token = response.context["form"].initial["review"]
    assert client.post(url(item), {"review": token, "accepted": "on"}).status_code == 302
    item.refresh_from_db()
    assert item.agreement_acceptance_id and not item.cart.invoice_id


@pytest.mark.parametrize("status", ["converted", "declined", "expired", "void"])
def test_closed_quote_cannot_receive_a_new_agreement_receipt(item, document, status):
    from apps.portal.cart_service import create_quote_from_cart
    values = acceptance(item)
    quote = create_quote_from_cart(item.cart)
    quote.status = status
    quote.save(update_fields=["status"])
    with pytest.raises(ValidationError, match="quote is closed"):
        accept_cart_agreements(**values)
    assert not DomainAgreementAcceptance.objects.exists()


def test_foreign_quote_association_cannot_receive_customer_acceptance(item, document, owner):
    from apps.portal.cart_service import create_quote_from_cart
    quote = create_quote_from_cart(item.cart)
    quote.user = owner
    quote.save(update_fields=["user"])
    with pytest.raises(ValidationError, match="quote is closed or needs support review"):
        cart_agreement_target(item.cart.user, item.pk)
