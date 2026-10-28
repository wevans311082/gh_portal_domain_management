import uuid
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.domains.models import DomainContactChange
from tests.test_contact_change_admission import current
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


def url(current):
    return reverse("domains:contact_change", args=[current[0].pk])


def data(current):
    return {"key": str(uuid.uuid4()), "publication_choice": "redact", "confirm": "on", **{
        role: current[1].registration_contact_id for role in ("registration_contact", "admin_contact", "tech_contact", "billing_contact")}}


def test_form_is_private_owned_and_explicit_about_review(current, client):
    client.force_login(current[1].user)
    response = client.get(url(current))
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert b"Individuals can hold domains" in response.content
    assert b"60-day transfer lock" in response.content
    assert response.context["form"].fields["registration_contact"].initial == current[1].registration_contact_id


def test_post_queues_without_provider_work_and_duplicate_is_idempotent(current, client):
    client.force_login(current[1].user)
    payload = data(current)
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as provider:
        first = client.post(url(current), payload)
        second = client.post(url(current), payload)
    assert first.status_code == second.status_code == 302
    provider.assert_not_called()
    assert DomainContactChange.objects.count() == 1
    response = client.get(url(current))
    assert response.context["show_form"] is False
    assert b"Change in progress: Queued" in response.content


def test_missing_confirmation_cannot_queue(current, client):
    client.force_login(current[1].user)
    payload = data(current)
    del payload["confirm"]
    assert client.post(url(current), payload).status_code == 400
    assert not DomainContactChange.objects.exists()


def test_foreign_domain_is_not_disclosed(current, client, django_user_model):
    user = django_user_model.objects.create_user(email="foreign-viewer@example.test", password="testpass")
    client.force_login(user)
    assert client.get(url(current)).status_code == 404
    assert client.post(url(current), data(current)).status_code == 404


def test_unreviewed_source_shows_support_recovery_without_form(current, client):
    current[1].registration_attempt.delete()
    client.force_login(current[1].user)
    response = client.get(url(current))
    assert response.status_code == 200
    assert response.context["form"] is None
    assert b"Contact change needs review" in response.content
    assert client.post(url(current), data(current)).status_code == 409


def test_duplicate_role_parameters_are_rejected(current, client):
    client.force_login(current[1].user)
    payload = data(current)
    payload["registration_contact"] = [str(current[1].registration_contact_id)] * 2
    assert client.post(url(current), payload).status_code == 400
    assert not DomainContactChange.objects.exists()


def test_individual_publication_rejected_in_form(current, client):
    client.force_login(current[1].user)
    payload = data(current)
    payload["publication_choice"] = "publish"
    response = client.post(url(current), payload)
    assert response.status_code == 400
    assert b"Organization publication is available only" in response.content


def test_open_change_holds_sensitive_registrar_operations(current, client):
    from apps.domains.registrar_review import registrar_review_required
    client.force_login(current[1].user)
    assert client.post(url(current), data(current)).status_code == 302
    assert registrar_review_required(current[0])
    with patch("apps.domains.views.ResellerClubClient") as provider:
        response = client.post(reverse("domains:get_auth_code", args=[current[0].pk]))
    assert response.status_code == 409
    provider.assert_not_called()
    DomainContactChange.objects.filter(domain=current[0]).update(status="cancelled")
    assert not registrar_review_required(current[0])


def test_contact_change_requires_csrf_token(current):
    from django.test import Client
    client = Client(enforce_csrf_checks=True)
    client.force_login(current[1].user)
    assert client.post(url(current), data(current)).status_code == 403
    assert not DomainContactChange.objects.exists()


def test_contact_change_body_is_bounded_before_admission(current, client):
    client.force_login(current[1].user)
    assert client.post(url(current), data="x" * (33 * 1024), content_type="application/x-www-form-urlencoded").status_code == 413
    assert not DomainContactChange.objects.exists()
