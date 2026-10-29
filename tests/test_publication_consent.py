from tests.agreement_helpers import attach_agreement
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.db import DatabaseError

from apps.audit.models import AuditLog
from apps.domains.models import DomainContact, DomainOrder, RegistrantPublicationDecision, TLDPricing
from apps.domains.publication_consent import capture_decision, publication_snapshot, publication_attributes
from apps.domains.registration_submission import submit_registration
from apps.domains.transfer_submission import submit_transfer
from apps.portal.cart_service import add_domain_registration_item, add_domain_transfer_item, create_invoice_from_cart, CartPreflightError
from apps.billing.models import Invoice
from tests.test_registration_submission import order, provider, NS

pytestmark = pytest.mark.django_db


def organization(order):
    contact = order.registration_contact
    contact.registrant_type, contact.company = "organisation", "Example Trading Ltd"
    contact.save()
    return contact


def decision(order):
    return capture_decision(customer=order.user, contact=organization(order), domain_name=order.domain_name, choice="publish")


def test_individual_is_valid_without_company_and_defaults_to_no_publication(order, provider):
    attach_agreement(order)
    submit_registration(order.pk, "999", NS, client=provider)
    assert provider.register_domain.call_args.kwargs["registrant_org_consent"] is False
    assert not RegistrantPublicationDecision.objects.exists()


def test_explicit_request_consent_is_bound_and_audited_before_purchase(order, provider):
    order.publication_decision = decision(order)
    order.save()
    attach_agreement(order)
    submit_registration(order.pk, "999", NS, client=provider)
    assert provider.register_domain.call_args.kwargs["registrant_org_consent"] is True
    assert AuditLog.objects.filter(action="domain.publication.consent_captured", user=order.user).count() == 1
    assert order.registration_attempt.subject["publication"]["consent"] is True
    assert order.user.email not in str(order.registration_attempt.subject)


@pytest.mark.parametrize("change", ["company", "name", "email", "city", "policy", "domain", "owner"])
def test_mismatched_or_stale_consent_blocks_before_contact_mutation(order, provider, change, django_user_model):
    evidence = decision(order)
    order.publication_decision = evidence
    order.save()
    if change in {"company", "name", "email", "city"}:
        DomainContact.objects.filter(pk=order.registration_contact_id).update(**{change: "different@example.test" if change == "email" else "Changed value"})
    elif change == "policy":
        RegistrantPublicationDecision.objects.filter(pk=evidence.pk).update(policy_version="unrecognized")
    elif change == "domain":
        RegistrantPublicationDecision.objects.filter(pk=evidence.pk).update(domain_name="other.com")
    else:
        other = django_user_model.objects.create_user(email="other-consent@example.test", password="testpass")
        RegistrantPublicationDecision.objects.filter(pk=evidence.pk).update(customer=other)
    with pytest.raises(ValidationError, match="consent is stale"):
        submit_registration(order.pk, "999", NS, client=provider)
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()


def test_consent_change_during_preparation_aborts_before_paid_post(order, provider):
    evidence = decision(order)
    order.publication_decision = evidence
    order.save()
    def create(payload):
        from django.utils import timezone
        RegistrantPublicationDecision.objects.filter(pk=evidence.pk).update(withdrawn_at=timezone.now())
        return "201"
    provider.create_contact.side_effect = create
    attach_agreement(order)
    with pytest.raises(ValidationError, match="details changed"):
        submit_registration(order.pk, "999", NS, client=provider)
    provider.register_domain.assert_not_called()
    assert order.registration_attempt.status == "aborted"


@pytest.mark.parametrize("choice", [True, "true", "yes", "1", "", None])
def test_only_exact_publication_choices_are_accepted(order, choice):
    with pytest.raises(ValidationError):
        capture_decision(customer=order.user, contact=organization(order), domain_name=order.domain_name, choice=choice)
    assert not RegistrantPublicationDecision.objects.exists()


def test_staff_cannot_infer_customer_consent(order, django_user_model):
    staff = django_user_model.objects.create_user(email="consent-staff@example.test", password="testpass", is_staff=True)
    with pytest.raises(ValidationError, match="Only the active customer"):
        capture_decision(customer=order.user, contact=organization(order), domain_name=order.domain_name, choice="publish", actor=staff)
    assert capture_decision(customer=order.user, contact=organization(order), domain_name=order.domain_name, actor=staff) is None


@pytest.mark.parametrize("domain", ["example.io", "example.co.uk", "example.uk", "example.ca"])
def test_unsupported_publication_extensions_are_rejected(order, domain):
    with pytest.raises(ValidationError, match="reviewed generic extension"):
        capture_decision(customer=order.user, contact=organization(order), domain_name=domain, choice="publish")
    assert publication_attributes(domain, False) == {}


@pytest.mark.parametrize("consent,expected", [(False, "false"), (True, "true")])
def test_transport_emits_exact_organization_consent_attribute(consent, expected):
    from apps.domains.resellerclub_client import ResellerClubClient
    client = object.__new__(ResellerClubClient)
    client._post = Mock(return_value={"entityid": "501"})
    client.register_domain("example.com", 1, "999", "1", "2", "3", "4", NS, registrant_org_consent=consent)
    assert client._post.call_args.args[1]["attr-name1"] == "registrant-org-consent"
    assert client._post.call_args.args[1]["attr-value1"] == expected
    client.transfer_domain("example.com", "999", "1", "2", "3", "4", NS, registrant_org_consent=consent)
    assert client._post.call_args.args[1]["attr-value1"] == expected
    with pytest.raises(ValidationError):
        client.register_domain("example.com", 1, "999", "1", "2", "3", "4", NS, registrant_org_consent="false")


def test_capture_audit_failure_rolls_back_consent_evidence(order):
    with patch.object(AuditLog.objects, "create", side_effect=DatabaseError("failed")), pytest.raises(DatabaseError):
        decision(order)
    assert not RegistrantPublicationDecision.objects.exists()


@pytest.mark.parametrize("transfer", [False, True])
def test_cart_retains_consent_through_checkout_and_paid_submission(order, provider, transfer):
    contact = organization(order)
    name = "cart-consent.com"
    TLDPricing.objects.create(tld="com", registration_cost=12, transfer_cost=12)
    add = add_domain_transfer_item if transfer else add_domain_registration_item
    item = add(user=order.user, domain_name=name, domain_contact_id=contact.pk, publication_choice="publish")
    from tests.agreement_helpers import accept_cart_item
    accept_cart_item(item)
    invoice = create_invoice_from_cart(item.cart, send_email=False)
    invoice.status, invoice.amount_paid = "paid", invoice.total
    invoice.save()
    request = (invoice.domain_transfers if transfer else invoice.domain_orders).get()
    assert request.publication_decision_id == item.publication_decision_id
    if transfer:
        provider.transfer_domain.return_value = {"entityid": "501"}
        submit_transfer(request.pk, "999", NS, client=provider)
        assert provider.transfer_domain.call_args.kwargs["registrant_org_consent"] is True
    else:
        submit_registration(request.pk, "999", NS, client=provider)
        assert provider.register_domain.call_args.kwargs["registrant_org_consent"] is True


def test_checkout_does_not_create_invoice_after_publication_contact_edits(order):
    contact = organization(order)
    TLDPricing.objects.create(tld="com", registration_cost=12)
    item = add_domain_registration_item(user=order.user, domain_name="changed-consent.com", domain_contact_id=contact.pk, publication_choice="publish")
    DomainContact.objects.filter(pk=contact.pk).update(company="Different Organization")
    with pytest.raises(CartPreflightError, match="consent is stale"):
        create_invoice_from_cart(item.cart)
    assert not Invoice.objects.exists()


def test_registration_form_rejects_individual_publication_before_invoice(order, client):
    TLDPricing.objects.create(tld="com", registration_cost=12)
    client.force_login(order.user)
    response = client.post(reverse("domains:register"), {"domain_name": "individual-consent.com", "contact": order.registration_contact_id,
        "registration_years": 1, "dns_provider": "cpanel", "publication_choice": "publish"})
    assert response.status_code == 200 and b"organization contact" in response.content
    assert not Invoice.objects.exists()


def test_withdrawal_is_idempotent_and_future_submission_uses_false(order, provider):
    from apps.domains.publication_consent import withdraw_decision
    order.publication_decision = decision(order)
    order.save()
    first = withdraw_decision(order.user, order.publication_decision_id)
    second = withdraw_decision(order.user, first.pk)
    assert first.withdrawn_at == second.withdrawn_at and second.withdrawal_ticket_id is None
    attach_agreement(order)
    submit_registration(order.pk, "999", NS, client=provider)
    assert provider.register_domain.call_args.kwargs["registrant_org_consent"] is False
    assert AuditLog.objects.filter(action="domain.publication.consent_withdrawn").count() == 1


def test_accepted_order_withdrawal_creates_one_tracked_provider_redaction_request(order, provider):
    from apps.domains.publication_consent import withdraw_decision
    from apps.support.models import SupportTicket
    order.publication_decision = decision(order)
    order.save()
    attach_agreement(order)
    submit_registration(order.pk, "999", NS, client=provider)
    first = withdraw_decision(order.user, order.publication_decision_id)
    second = withdraw_decision(order.user, first.pk)
    assert first.withdrawal_ticket_id == second.withdrawal_ticket_id and SupportTicket.objects.count() == 1
    assert "not confirmation of provider redaction" in SupportTicket.objects.get().messages.get().content
    assert provider.register_domain.call_count == 1


def test_withdrawal_audit_failure_rolls_back_ticket_and_decision(order, provider):
    from apps.domains.publication_consent import withdraw_decision
    from apps.support.models import SupportTicket
    order.publication_decision = decision(order)
    order.save()
    attach_agreement(order)
    submit_registration(order.pk, "999", NS, client=provider)
    with patch.object(AuditLog.objects, "create", side_effect=DatabaseError("failed")), pytest.raises(DatabaseError):
        withdraw_decision(order.user, order.publication_decision_id)
    assert not SupportTicket.objects.exists()
    assert RegistrantPublicationDecision.objects.get().withdrawn_at is None


def test_publication_views_are_owned_private_and_post_only(order, client, django_user_model):
    evidence = decision(order)
    client.force_login(order.user)
    url = reverse("domains:publication_withdraw", args=[evidence.pk])
    assert client.get(url).status_code == 405
    response = client.get(reverse("domains:publication_decisions"))
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert order.domain_name in response.content.decode()
    other = django_user_model.objects.create_user(email="other-publication@example.test", password="testpass")
    client.force_login(other)
    assert client.post(url).status_code == 404
    assert order.domain_name not in client.get(reverse("domains:publication_decisions")).content.decode()


def test_publication_withdrawal_requires_csrf(order):
    from django.test import Client
    evidence = decision(order)
    client = Client(enforce_csrf_checks=True)
    client.force_login(order.user)
    assert client.post(reverse("domains:publication_withdraw", args=[evidence.pk])).status_code == 403
    evidence.refresh_from_db()
    assert evidence.withdrawn_at is None


def test_duplicate_publication_parameters_do_not_capture_consent(order, client):
    from django.http import QueryDict
    TLDPricing.objects.create(tld="com", registration_cost=12)
    contact = organization(order)
    client.force_login(order.user)
    data = QueryDict(mutable=True)
    data.update({"domain_name": "duplicate-choice.com", "contact": contact.pk, "registration_years": 1, "dns_provider": "cpanel"})
    data.setlist("publication_choice", ["redact", "publish"])
    response = client.post(reverse("domains:register"), data.urlencode(), content_type="application/x-www-form-urlencoded")
    assert response.status_code == 200 and b"Submit one organization" in response.content
    assert not Invoice.objects.exists() and not RegistrantPublicationDecision.objects.exists()


def test_cached_contact_cannot_keep_consent_current_after_database_edit(order):
    evidence = decision(order)
    order.publication_decision = evidence
    order.save()
    cached = order.registration_contact
    DomainContact.objects.filter(pk=cached.pk).update(company="Different Organization")
    with pytest.raises(ValidationError, match="consent is stale"):
        publication_snapshot(order, cached)


def test_capture_cannot_silently_authorize_an_edited_organization(order):
    contact = organization(order)
    DomainContact.objects.filter(pk=contact.pk).update(company="Unseen Organization")
    with pytest.raises(ValidationError, match="contact changed"):
        capture_decision(customer=order.user, contact=contact, domain_name=order.domain_name, choice="publish")
    assert not RegistrantPublicationDecision.objects.exists()


def test_capture_rechecks_active_customer_instead_of_cached_actor(order):
    contact = organization(order)
    type(order.user).objects.filter(pk=order.user_id).update(is_active=False)
    with pytest.raises(ValidationError, match="active customer"):
        capture_decision(customer=order.user, contact=contact, domain_name=order.domain_name, choice="publish")
    assert not RegistrantPublicationDecision.objects.exists()
