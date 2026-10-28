import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.domains.contact_bindings import provider_scope
from apps.domains.contact_changes import request_contact_change
from apps.domains.models import Domain, DomainContact, DomainContactChange, DomainRegistrationAttempt
from apps.domains.registrar_policy import ALL_ROLES
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


@pytest.fixture
def current(order):
    domain = Domain.objects.create(user=order.user, name=order.domain_name, tld="com", status="active", registrar_id="501")
    order.domain = domain
    order.status = "completed"
    order.registrar_order_id = "501"
    order.registrar_contact_ids = dict(reg="201", admin="201", tech="201", billing="201", _scope=provider_scope())
    order.save()
    DomainRegistrationAttempt.objects.create(order=order, status="accepted", provider_scope=provider_scope(),
        customer_id="999", remote_order_id="501")
    return domain, order


def request(current, **kwargs):
    domain, source = current
    values = dict(customer=source.user, domain_id=domain.pk, key=uuid.uuid4(),
                  contacts={role: source.registration_contact_id for role in ALL_ROLES})
    values.update(kwargs)
    return request_contact_change(**values)


def test_proposal_is_durable_private_and_does_not_change_current_association(current):
    domain, source = current
    contact = DomainContact.objects.get(pk=source.registration_contact_id)
    contact.pk = None
    contact.name = "New Holder"
    contact.save()
    roles = {role: contact.pk for role in ALL_ROLES}
    change = request(current, contacts=roles)
    source.refresh_from_db()
    assert change.status == "queued"
    assert source.registration_contact_id != contact.pk
    assert change.subject["roles"] == roles
    assert change.subject["previous_remote_ids"] == source.registrar_contact_ids
    assert change.subject["publication"]["consent"] is False
    assert contact.email not in str(change.subject)
    assert contact.name not in str(change.subject)
    assert change.customer_id == "999"


def test_same_key_is_idempotent_and_changed_proposal_is_rejected(current):
    key = uuid.uuid4()
    first = request(current, key=key)
    assert request(current, key=key).pk == first.pk
    contact = current[1].registration_contact
    contact.name = "Edited Holder"
    contact.save()
    with pytest.raises(ValidationError, match="already been used"):
        request(current, key=key)
    assert DomainContactChange.objects.count() == 1


@pytest.mark.parametrize("state", ["queued", "preparing", "submitting", "awaiting", "unknown"])
def test_open_and_uncertain_changes_cannot_be_replaced(current, state):
    first = request(current)
    DomainContactChange.objects.filter(pk=first.pk).update(status=state)
    with pytest.raises(ValidationError, match="already open"):
        request(current)


@pytest.mark.parametrize("field,value", [("status", "processing"), ("registrar_order_id", "502"),
    ("registrar_contact_ids", {}), ("registrar_contact_ids", {"_scope": "foreign"})])
def test_unproven_source_cannot_queue_change(current, field, value):
    source = current[1]
    setattr(source, field, value)
    source.save()
    with pytest.raises(ValidationError, match="review"):
        request(current)
    assert not DomainContactChange.objects.exists()


def test_inactive_customer_cannot_queue_change(current):
    user = current[1].user
    user.is_active = False
    user.save()
    with pytest.raises(PermissionDenied):
        request(current)


def test_missing_role_and_boolean_identifier_are_rejected(current):
    for contacts in ({}, {role: True for role in ALL_ROLES}):
        with pytest.raises(ValidationError):
            request(current, contacts=contacts)
    assert not DomainContactChange.objects.exists()


def test_individual_cannot_authorize_organization_publication(current):
    with pytest.raises(ValidationError, match="Organization publication"):
        request(current, publication_choice="publish")
    assert not DomainContactChange.objects.exists()


def test_changed_provider_scope_cannot_retarget_request(current):
    attempt = current[1].registration_attempt
    attempt.provider_scope = "foreign"
    attempt.save()
    with pytest.raises(ValidationError, match="review"):
        request(current)


@pytest.mark.parametrize("changes", [{"status": "unknown"}, {"legacy_review_required": True},
    {"remote_order_id": "502"}, {"customer_id": ""}])
def test_unconfirmed_attempt_cannot_supply_current_provider_identity(current, changes):
    DomainRegistrationAttempt.objects.filter(order=current[1]).update(**changes)
    with pytest.raises(ValidationError, match="review"):
        request(current)


def test_foreign_contact_is_rejected(current, django_user_model):
    source = current[1]
    other = django_user_model.objects.create_user(email="other-holder@example.test", password="testpass")
    contact = source.registration_contact
    contact.pk = None
    contact.user = other
    contact.save()
    with pytest.raises(PermissionDenied):
        request(current, contacts={role: contact.pk for role in ALL_ROLES})
    assert not DomainContactChange.objects.exists()
