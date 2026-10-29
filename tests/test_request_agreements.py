import time
import uuid
from unittest.mock import patch

import pytest
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.agreements import current_agreement_plan, publish_agreement
from apps.domains.models import ContactVerification, DomainAgreementAcceptance, DomainOrder
from apps.domains.request_agreements import REQUESTS, accept_request_agreements, request_agreement_target
from tests.test_registrar_agreements import document, publication, owner, order
from tests.test_transfer_submission import transfer

pytestmark = pytest.mark.django_db


@pytest.fixture(params=["registration", "transfer"])
def target(request):
    return request.getfixturevalue("order" if request.param == "registration" else "transfer")


def kind(target):
    return "registration" if isinstance(target, DomainOrder) else "transfer"


def url(target):
    return reverse("domains:request_agreements", kwargs={"kind": kind(target), "pk": target.pk})


def acceptance(target, **changes):
    item, contact, operation, digest = request_agreement_target(target.user, target.pk, kind=kind(target))
    values = dict(customer=target.user, request_id=target.pk, kind=operation, context_digest=digest,
        key=uuid.uuid4(), subject_digest=ContactVerification.digest_for(contact),
        plan_digest=current_agreement_plan(target.domain_name, operation).digest, accepted=True)
    values.update(changes)
    return values


def test_direct_review_is_read_only_escaped_and_displays_actual_registrant(client, target, document):
    client.force_login(target.user)
    response = client.get(url(target))
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    content = response.content.decode()
    assert "&lt;script&gt;plain text&lt;/script&gt;" in content and target.registration_contact.email in content
    assert target.registration_contact.phone in content and "PRIVATE-AUTH-CODE" not in content
    assert "Back to request agreements" in content
    assert not DomainAgreementAcceptance.objects.exists()
    target.refresh_from_db()
    assert target.agreement_acceptance_id is None


def test_direct_acceptance_attaches_exact_receipt_and_post_replays_once(client, target, document):
    client.force_login(target.user)
    token = client.get(url(target)).context["form"].initial["review"]
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as provider:
        for _ in range(2):
            response = client.post(url(target), {"review": token, "accepted": "on"})
            assert response.status_code == 302 and response.url == url(target)
    provider.assert_not_called()
    target.refresh_from_db()
    receipt = target.agreement_acceptance
    assert receipt.customer_id == target.user_id and receipt.operation == kind(target)
    assert receipt.documents.get().document_id == document.pk
    assert DomainAgreementAcceptance.objects.count() == 1
    assert AuditLog.objects.filter(action="domain.agreement.attached", model_name=type(target).__name__).count() == 1


@pytest.mark.parametrize("state", ["cancelled", "completed", "paused"])
def test_closed_or_paused_request_cannot_accept(target, document, state):
    values = acceptance(target)
    type(target).objects.filter(pk=target.pk).update(status=state)
    with pytest.raises(ValidationError, match="closed, already admitted"):
        accept_request_agreements(**values)
    assert not DomainAgreementAcceptance.objects.exists()


@pytest.mark.parametrize("state,submitted,historical", [
    ("preparing", False, False), ("submitting", True, False), ("unknown", True, False),
    ("accepted", True, False), ("aborted", True, False), ("aborted", False, True)])
def test_admitted_uncertain_and_historical_attempts_cannot_replace_history(target, document, state, submitted, historical):
    values = acceptance(target)
    _, attempt_model, relation = REQUESTS[kind(target)]
    attempt_model.objects.create(**{relation: target}, status=state,
        submitted_at=timezone.now() if submitted else None, legacy_review_required=historical)
    with pytest.raises(ValidationError, match="history cannot be replaced"):
        accept_request_agreements(**values)
    assert not DomainAgreementAcceptance.objects.exists()


def test_unsubmitted_aborted_attempt_permits_new_review(target, document):
    _, attempt_model, relation = REQUESTS[kind(target)]
    attempt_model.objects.create(**{relation: target}, status="aborted")
    receipt = accept_request_agreements(**acceptance(target))
    target.refresh_from_db()
    assert target.agreement_acceptance_id == receipt.pk


@pytest.mark.parametrize("field,value", [("registrar_order_id", "501"), ("total_price", "999.00"),
    ("domain_name", "edited-request.com"), ("tld", "io")])
def test_request_changes_after_review_do_not_attach_receipt(target, document, field, value):
    values = acceptance(target)
    type(target).objects.filter(pk=target.pk).update(**{field: value})
    with pytest.raises(ValidationError):
        accept_request_agreements(**values)
    assert not DomainAgreementAcceptance.objects.exists()


def test_contact_edit_requires_fresh_review(target, document):
    values = acceptance(target)
    contact = target.registration_contact
    contact.name = "Another registrant"
    contact.save(update_fields=["name"])
    with pytest.raises(ValidationError, match="contact changed"):
        accept_request_agreements(**values)


def test_old_replay_does_not_replace_receipt_for_new_terms(target, document, owner):
    old_values = acceptance(target)
    accept_request_agreements(**old_values)
    publish_agreement(actor=owner, **publication(version="test-v2"))
    current = accept_request_agreements(**acceptance(target))
    with pytest.raises(ValidationError, match="Current customer acceptance"):
        accept_request_agreements(**old_values)
    target.refresh_from_db()
    assert target.agreement_acceptance_id == current.pk


def test_attachment_audit_rollback_preserves_no_partial_receipt(target, document):
    values = acceptance(target)
    original = AuditLog.objects.create
    def audit(**kwargs):
        if kwargs["action"] == "domain.agreement.attached":
            raise DatabaseError("test attachment failure")
        return original(**kwargs)
    with patch.object(AuditLog.objects, "create", side_effect=audit), pytest.raises(DatabaseError):
        accept_request_agreements(**values)
    target.refresh_from_db()
    assert target.agreement_acceptance_id is None and not DomainAgreementAcceptance.objects.exists()


def test_revoked_customer_is_rechecked_before_writing(target, document):
    values = acceptance(target)
    type(target.user).objects.filter(pk=target.user_id).update(is_staff=True)
    with pytest.raises(PermissionDenied):
        accept_request_agreements(**values)
    assert not DomainAgreementAcceptance.objects.exists()


def test_foreign_customer_and_staff_cannot_review(client, target, document, owner, django_user_model):
    client.force_login(owner)
    assert client.get(url(target)).status_code == 403
    other = django_user_model.objects.create_user(email="foreign-request@example.test", password="testpass")
    client.force_login(other)
    assert client.get(url(target)).status_code == 404
    assert client.post(url(target), {"review": "invalid", "accepted": "on"}).status_code == 404


def test_expired_or_wrong_kind_review_does_not_accept(client, target, document):
    from apps.domains.agreement_views import SALT
    client.force_login(target.user)
    token = client.get(url(target)).context["form"].initial["review"]
    with patch("django.core.signing.time.time", return_value=time.time() + 1801):
        response = client.post(url(target), {"review": token, "accepted": "on"})
    assert response.status_code == 400 and not response.context["form"].is_bound
    data = signing.loads(token, salt=SALT)
    data["kind"] = "cart"
    response = client.post(url(target), {"review": signing.dumps(data, salt=SALT), "accepted": "on"})
    assert response.status_code == 400 and not DomainAgreementAcceptance.objects.exists()


def test_changed_terms_are_shown_with_cleared_approval(client, target, document, owner):
    client.force_login(target.user)
    token = client.get(url(target)).context["form"].initial["review"]
    publish_agreement(actor=owner, **publication(version="test-v2"))
    response = client.post(url(target), {"review": token, "accepted": "on"})
    assert response.status_code == 400 and not response.context["form"].is_bound
    assert b"Version test-v2" in response.content and not DomainAgreementAcceptance.objects.exists()


def test_request_list_is_owned_paginated_and_omits_admitted_history(client, order, transfer, document):
    client.force_login(order.user)
    for number in range(22):
        DomainOrder.objects.create(user=order.user, domain_name=f"listed-{number}.com", tld="com",
            registration_contact=order.registration_contact, admin_contact=order.admin_contact,
            tech_contact=order.tech_contact, billing_contact=order.billing_contact)
    response = client.get(reverse("domains:agreement_requests"))
    assert "no-store" in response["Cache-Control"]
    assert response.context["request_pages"][0]["page"].paginator.count == 23
    assert len(response.context["request_pages"][0]["page"].object_list) == 20
    assert transfer.domain_name.encode() not in response.content
    assert b"registration_page=2" in response.content
    order.registrar_order_id = "501"
    order.save(update_fields=["registrar_order_id"])
    response = client.get(reverse("domains:agreement_requests"), {"registration_page": 2})
    assert order.domain_name.encode() not in response.content


def test_impersonation_cannot_list_or_accept_direct_request(target, document, owner):
    from django.test import RequestFactory
    from apps.domains.agreement_views import cart_agreements, request_list
    request = RequestFactory().get(url(target))
    request.user = target.user
    request.session = {"owner_impersonation": {"owner_id": owner.pk}}
    request.impersonation_actor = owner
    with pytest.raises(PermissionDenied):
        cart_agreements(request, target.pk, kind=kind(target))
    with pytest.raises(PermissionDenied):
        request_list(request)


def test_us_workstream_review_shows_actual_saved_declaration(client, order, document):
    contact = order.registration_contact
    contact.us_nexus_category = "C11"
    contact.us_application_purpose = "P3"
    contact.us_nexus_certified = True
    contact.save()
    order.domain_name, order.tld = "individual-agreements.us", "us"
    order.save(update_fields=["domain_name", "tld"])
    client.force_login(order.user)
    response = client.get(url(order))
    assert response.status_code == 200 and response.context["form"]
    assert b"US citizen (individual)" in response.content and b"Personal use" in response.content
    assert b"separate from independent eligibility verification" in response.content


@pytest.mark.parametrize("extension", ["eu", "unknown"])
def test_published_agreements_do_not_enable_unreviewed_request_workflows(client, order, document, extension):
    order.domain_name, order.tld = "held-agreements." + extension, extension
    order.save(update_fields=["domain_name", "tld"])
    client.force_login(order.user)
    response = client.get(url(order))
    assert response.status_code == 200 and "form" not in response.context
    assert not DomainAgreementAcceptance.objects.exists()


def test_direct_route_csrf_body_bounds_and_duplicate_fields(client, order, document):
    from django.test import Client
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(order.user)
    assert strict.post(url(order), {}).status_code == 403
    client.force_login(order.user)
    token = client.get(url(order)).context["form"].initial["review"]
    assert client.post(url(order), {"review": [token, token], "accepted": "on"}).status_code == 400
    assert client.post(url(order), data="x" * (33 * 1024), content_type="application/x-www-form-urlencoded").status_code == 413
    assert not DomainAgreementAcceptance.objects.exists()
