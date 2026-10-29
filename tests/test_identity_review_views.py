import uuid

import pytest
from django.test import Client
from django.urls import reverse

from apps.domains.models import ContactVerification
from tests.security_helpers import login_owner
from tests.test_contact_change_recovery import owner
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


def url(order):
    return reverse("admin_tools:identity_review", args=[order.user_id, order.registration_contact_id])


def payload(order):
    return dict(key=str(uuid.uuid4()), subject_digest=ContactVerification.digest_for(order.registration_contact), status="verified", check_kind="identity",
        policy_reference="POLICY-IDENTITY-v1", evidence_reference="CASE-100", valid_days=90, policy_reviewed="on")


def test_owner_can_record_individual_local_review_and_repeat_is_idempotent(order, owner, client):
    login_owner(client, owner)
    response = client.get(url(order))
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    data = payload(order)
    assert client.post(url(order), data).status_code == 302
    assert client.post(url(order), data).status_code == 302
    assert ContactVerification.objects.filter(check_kind="identity").count() == 1
    assert b"CASE-100" in client.get(url(order)).content


def test_ordinary_staff_cannot_read_references_or_submit(order, client, django_user_model):
    staff = django_user_model.objects.create_user(email="identity-staff@example.test", password="testpass", is_staff=True)
    client.force_login(staff)
    assert client.get(url(order)).status_code == 403
    assert client.post(url(order), payload(order)).status_code == 403
    assert not ContactVerification.objects.exists()


def test_owner_without_recent_mfa_cannot_submit(order, owner, client):
    client.force_login(owner)
    response = client.post(url(order), payload(order))
    assert response.status_code == 403 or response.status_code == 302
    assert not ContactVerification.objects.exists()


def test_identity_review_rejects_stale_contact_version(order, owner, client):
    login_owner(client, owner)
    data = payload(order)
    contact = order.registration_contact
    contact.name = "Changed Contact"
    contact.save()
    response = client.post(url(order), data)
    assert response.status_code == 400 and b"Review the current version" in response.content
    assert not ContactVerification.objects.exists()


def test_identity_review_requires_csrf(order, owner):
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    assert client.post(url(order), payload(order)).status_code == 403


def test_contact_cannot_be_reviewed_under_another_customer(order, owner, client, django_user_model):
    login_owner(client, owner)
    other = django_user_model.objects.create_user(email="identity-other@example.test", password="testpass")
    assert client.get(reverse("admin_tools:identity_review", args=[other.pk, order.registration_contact_id])).status_code == 404
