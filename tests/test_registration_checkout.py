import pytest
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.billing.models import Invoice
from apps.domains.models import DomainOrder
from apps.domains.registration_checkout import create_registration_invoice
from apps.domains.request_agreements import request_agreement_target
from tests.agreement_helpers import attach_agreement, publish_test_agreement
from tests.test_registration_submission import order
from tests.test_registrar_agreements import publication, owner

pytestmark = pytest.mark.django_db


@pytest.fixture
def draft(order):
    order.status, order.total_price = "draft", "12.00"
    order.save(update_fields=["status", "total_price"])
    order.refresh_from_db()
    return order


def values(draft):
    from apps.domains.agreements import current_agreement_plan
    from apps.domains.models import ContactVerification
    target, contact, _, digest = request_agreement_target(draft.user, draft.pk, kind="registration")
    return dict(customer=draft.user, order_id=draft.pk, context_digest=digest,
        plan_digest=current_agreement_plan(draft.domain_name, "registration").digest,
        subject_digest=ContactVerification.digest_for(contact), send_email=False)


def test_missing_acceptance_preserves_draft_without_invoice(draft):
    publish_test_agreement()
    with pytest.raises(ValidationError, match="Current customer acceptance"):
        create_registration_invoice(**values(draft))
    draft.refresh_from_db()
    assert draft.status == "draft" and draft.invoice_id is None and not Invoice.objects.exists()


def test_exact_billing_contract_and_invoice_replay_are_retained(draft, owner):
    attach_agreement(draft)
    args = values(draft)
    first = create_registration_invoice(**args)
    from apps.domains.agreements import publish_agreement
    publish_agreement(actor=owner, **publication(version="after-draft-invoicing"))
    assert create_registration_invoice(**args).pk == first.pk
    assert first.total == 12 and first.vat_rate == 0
    assert first.source_kind == Invoice.SOURCE_DOMAIN_REGISTRATION
    assert Invoice.objects.count() == 1


def test_price_change_since_review_requires_new_confirmation(draft):
    attach_agreement(draft)
    args = values(draft)
    DomainOrder.objects.filter(pk=draft.pk).update(total_price="99.00")
    with pytest.raises(ValidationError, match="price changed"):
        create_registration_invoice(**args)
    assert not Invoice.objects.exists()


def test_invoice_audit_failure_rolls_back_all_financial_writes(draft):
    attach_agreement(draft)
    args = values(draft)
    with patch.object(AuditLog.objects, "create", side_effect=DatabaseError), pytest.raises(DatabaseError):
        create_registration_invoice(**args)
    draft.refresh_from_db()
    assert draft.invoice_id is None and draft.status == "draft" and not Invoice.objects.exists()


def test_stale_terms_cannot_be_invoiced_from_an_old_review(draft, owner):
    from apps.domains.agreements import publish_agreement
    attach_agreement(draft)
    args = values(draft)
    publish_agreement(actor=owner, **publication(version="before-draft-invoicing"))
    with pytest.raises(ValidationError):
        create_registration_invoice(**args)
    assert not Invoice.objects.exists()


def test_direct_invoice_http_rejects_price_fields_and_requires_csrf(client, draft):
    from django.test import Client
    attach_agreement(draft)
    client.force_login(draft.user)
    url = reverse("domains:registration_invoice", args=[draft.pk])
    assert client.get(url).status_code == 405
    review_url = reverse("domains:request_agreements", kwargs={"kind": "registration", "pk": draft.pk})
    token = client.get(review_url).context["form"].initial["review"]
    assert client.post(url, {"review": token, "total_price": "0.01"}).status_code == 400
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(draft.user)
    assert strict.post(url, {"review": token}).status_code == 403
    assert not Invoice.objects.exists()


def test_duplicate_registration_draft_does_not_create_invoice_or_replace_receipt(client, draft):
    from apps.domains.models import TLDPricing
    TLDPricing.objects.create(tld="com", registration_cost=12, is_active=True)
    attach_agreement(draft)
    client.force_login(draft.user)
    response = client.post(reverse("domains:register"), {"domain_name": draft.domain_name,
        "contact": draft.registration_contact_id, "registration_years": 1, "dns_provider": "cpanel",
        "publication_choice": "redact"})
    assert response.status_code == 200 and b"A request already exists" in response.content
    assert DomainOrder.objects.count() == 1 and not Invoice.objects.exists()
    original = draft.agreement_acceptance_id
    draft.refresh_from_db()
    assert draft.agreement_acceptance_id == original
