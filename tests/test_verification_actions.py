from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
from types import SimpleNamespace
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.models import Domain, DomainContact, DomainOrder, VerificationAction
from apps.domains.verification_actions import (
    request_verification_action, run_verification_action, dispatch_verification_actions,
    publish_verification_action, VerificationBusy, VerificationReviewRequired,
)


@pytest.fixture
def domain(db, django_user_model):
    user = django_user_model.objects.create_user(email="actions@example.com", password="testpass")
    contact = DomainContact.objects.create(
        user=user, label="Home", registrant_type="individual", email=user.email, registrar_contact_id="123",
    )
    domain = Domain.objects.create(user=user, name="actions.com", tld="com", registrar_id="456")
    DomainOrder.objects.create(
        domain=domain, user=user, domain_name=domain.name, tld=domain.tld, registrar_order_id="456",
        registration_contact=contact, admin_contact=contact, tech_contact=contact, billing_contact=contact,
    )
    return domain


def enqueue(domain, kind="refresh", key=None):
    return request_verification_action(domain, domain.user, kind, key or uuid.uuid4())


def provider(domain, status="Pending"):
    return Mock(get_registrant_verification_details=Mock(return_value={
        "orderid": "456", "domainname": domain.name, "customerid": "999",
        "registrantcontactid": "123", "registrantcontact": {"email": domain.user.email},
        "raaVerificationStatus": status,
    }), resend_registrant_verification=Mock(return_value=True))


def drive(job, client):
    with patch("apps.domains.resellerclub_client.ResellerClubClient", return_value=client), patch(
        "apps.core.runtime_settings.get_runtime_setting", return_value="999"
    ):
        run_verification_action.run(job.pk)
    job.refresh_from_db()


def test_request_is_durable_idempotent_and_does_not_call_provider(domain):
    key = uuid.uuid4()
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as client:
        job = enqueue(domain, key=key)
        assert enqueue(domain, key=key).pk == job.pk
        client.assert_not_called()
    assert job.status == "queued"
    assert VerificationAction.objects.count() == 1
    assert AuditLog.objects.filter(action="domain.verification.requested").count() == 1


def test_queued_action_is_cancelled_after_order_contact_binding_changes(domain):
    order = domain.order
    order.registrar_contact_ids = {"reg": "123"}
    order.save(update_fields=["registrar_contact_ids"])
    job = enqueue(domain)
    assert job.remote_contact_id == "123"
    order.registrar_contact_ids = {"reg": "789"}
    order.save(update_fields=["registrar_contact_ids"])
    client = provider(domain)
    drive(job, client)
    assert job.status == "cancelled"
    client.get_registrant_verification_details.assert_not_called()
    client.resend_registrant_verification.assert_not_called()


def test_queued_action_is_cancelled_after_provider_scope_changes(domain):
    order = domain.order
    order.registrar_contact_ids = {"reg": "123", "_scope": "original-scope"}
    order.save(update_fields=["registrar_contact_ids"])
    with patch("apps.domains.contact_bindings.provider_scope", return_value="original-scope"):
        job = enqueue(domain)
    order.registrar_contact_ids = {"reg": "123", "_scope": "replacement-scope"}
    order.save(update_fields=["registrar_contact_ids"])
    client = provider(domain)
    drive(job, client)
    assert job.status == "cancelled"
    client.get_registrant_verification_details.assert_not_called()


def test_conflicting_key_cannot_change_operation(domain):
    job = enqueue(domain)
    with pytest.raises(ValidationError):
        enqueue(domain, "resend", job.key)


def test_foreign_actor_is_rejected(domain, django_user_model):
    other = django_user_model.objects.create_user(email="foreign-action@example.com", password="testpass")
    with pytest.raises(ValidationError):
        request_verification_action(domain, other, "refresh", uuid.uuid4())
    assert not VerificationAction.objects.exists()


def test_pending_and_recent_actions_are_bounded(domain):
    job = enqueue(domain)
    with pytest.raises(VerificationBusy):
        enqueue(domain)
    VerificationAction.objects.filter(pk=job.pk).update(status="succeeded")
    with pytest.raises(VerificationBusy):
        enqueue(domain)


def test_audit_failure_rolls_back_admission(domain):
    with patch("apps.domains.verification_actions._audit", side_effect=DatabaseError("private")):
        with pytest.raises(DatabaseError):
            enqueue(domain)
    assert not VerificationAction.objects.exists()


def test_eager_publication_does_not_execute_provider_work(domain, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    job = enqueue(domain)
    with patch.object(run_verification_action, "delay") as task:
        assert publish_verification_action(job.pk) is False
        task.assert_not_called()


def test_broker_failure_retains_queued_row(domain, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = False
    job = enqueue(domain)
    with patch.object(run_verification_action, "delay", side_effect=RuntimeError("broker unavailable")):
        assert not publish_verification_action(job.pk)
    job.refresh_from_db()
    assert job.status == "queued"


def test_refresh_never_sends_mail_and_duplicate_worker_is_noop(domain):
    job = enqueue(domain)
    client = provider(domain)
    drive(job, client)
    assert job.status == "succeeded"
    drive(job, client)
    client.get_registrant_verification_details.assert_called_once()
    client.resend_registrant_verification.assert_not_called()


@pytest.mark.parametrize("status,expected", [("Pending", "succeeded"), ("Suspended", "succeeded"), ("Verified", "cancelled"), ("unknown", "cancelled")])
def test_resend_requires_current_pending_provider_state(domain, status, expected):
    job = enqueue(domain, "resend")
    client = provider(domain, status)
    drive(job, client)
    assert job.status == expected
    assert client.resend_registrant_verification.call_count == (1 if expected == "succeeded" else 0)


def test_uncertain_resend_is_never_automatically_repeated(domain):
    job = enqueue(domain, "resend")
    client = provider(domain)
    client.resend_registrant_verification.side_effect = TimeoutError("private response")
    drive(job, client)
    assert job.status == "unknown"
    drive(job, client)
    client.resend_registrant_verification.assert_called_once_with("456")
    with pytest.raises(VerificationReviewRequired):
        enqueue(domain, "resend")
    assert "private response" not in str(list(AuditLog.objects.values("data")))


def test_explicit_negative_result_is_failed_without_repeat(domain):
    job = enqueue(domain, "resend")
    client = provider(domain)
    client.resend_registrant_verification.return_value = False
    drive(job, client)
    assert job.status == "failed"
    drive(job, client)
    assert client.resend_registrant_verification.call_count == 1


def test_changed_contact_cancels_queued_job_before_provider(domain):
    job = enqueue(domain, "resend")
    DomainContact.objects.filter(pk=domain.order.registration_contact_id).update(email="changed@example.com")
    client = provider(domain)
    drive(job, client)
    assert job.status == "cancelled"
    client.get_registrant_verification_details.assert_not_called()


def test_expired_resend_lease_requires_review(domain):
    job = enqueue(domain, "resend")
    VerificationAction.objects.filter(pk=job.pk).update(status="running", lease_until=timezone.now() - timedelta(seconds=1))
    with patch("apps.domains.verification_actions.publish_verification_action") as publish:
        dispatch_verification_actions.run()
    job.refresh_from_db()
    assert job.status == "unknown"
    publish.assert_not_called()


def test_owner_review_resolves_outcome_without_sending_or_verifying(domain, django_user_model):
    owner = django_user_model.objects.create_superuser(email="review-owner@example.com", password="testpass")
    job = enqueue(domain, "resend")
    VerificationAction.objects.filter(pk=job.pk).update(status="unknown")
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as client:
        call_command("resolve_verification_action", action_id=job.pk, owner_id=owner.pk,
                     outcome="not-sent", reason="Provider confirmed no email was sent", provider_reference="CASE-123", stdout=StringIO())
        client.assert_not_called()
    job.refresh_from_db()
    assert job.status == "failed"
    assert not domain.order.verification_evidence.exists()


def test_http_requires_post_and_ownership(domain, client, django_user_model):
    client.force_login(domain.user)
    url = reverse("domains:verification_action", kwargs={"pk": domain.pk})
    assert client.get(url).status_code == 405
    other = django_user_model.objects.create_user(email="http-foreign@example.com", password="testpass")
    client.force_login(other)
    assert client.post(url, {"kind": "refresh", "key": str(uuid.uuid4())}).status_code == 404
    assert not VerificationAction.objects.exists()


def test_http_queues_without_provider_call_and_explains_cooldown(domain, client):
    client.force_login(domain.user)
    url = reverse("domains:verification_action", kwargs={"pk": domain.pk})
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as provider_client:
        assert client.post(url, {"kind": "refresh", "key": str(uuid.uuid4())}).status_code == 302
        response = client.post(url, {"kind": "refresh", "key": str(uuid.uuid4())})
        provider_client.assert_not_called()
    assert response.status_code == 429
    assert response["Retry-After"]
    assert VerificationAction.objects.count() == 1


def test_resend_api_only_accepts_boolean_success():
    from apps.domains.resellerclub_client import ResellerClubClient, ResellerClubError
    client = object.__new__(ResellerClubClient)
    client._post = Mock(return_value=True)
    assert client.resend_registrant_verification("456") is True
    client._post.assert_called_once_with("domains/raa/resend-verification", {"order-id": "456"})
    client._post.return_value = {"success": True}
    with pytest.raises(ResellerClubError):
        client.resend_registrant_verification("456")


@pytest.mark.parametrize("value", [True, False])
def test_http_transport_preserves_boolean_response_instead_of_contact_id(value):
    from apps.domains.resellerclub_client import ResellerClubClient

    client = object.__new__(ResellerClubClient)
    client.base_url = "https://registrar.invalid/api"
    client._auth_params = {}
    client.ensure_configured = Mock()
    client._capture_debug = Mock()
    response = SimpleNamespace(
        text="true" if value else "false", json=Mock(return_value=value),
        raise_for_status=Mock(), status_code=200, reason="OK", headers={},
        request=SimpleNamespace(url="https://registrar.invalid/api/domains/raa/resend-verification.json", headers={}, body=""),
    )
    client.session = Mock(post=Mock(return_value=response))
    assert client.resend_registrant_verification("456") is value
    client.session.post.assert_called_once()


def test_global_pending_capacity_is_bounded(domain):
    orders = DomainOrder.objects.bulk_create([
        DomainOrder(user=domain.user, domain_name=f"backlog-{index}.com", tld="com",
                    registration_contact=domain.order.registration_contact, admin_contact=domain.order.admin_contact,
                    tech_contact=domain.order.tech_contact, billing_contact=domain.order.billing_contact)
        for index in range(500)
    ])
    VerificationAction.objects.bulk_create([
        VerificationAction(domain_order=order, actor=domain.user, key=uuid.uuid4(), kind="refresh",
                           subject_digest="0" * 64, registrar_order_id="123")
        for order in orders
    ])
    with pytest.raises(VerificationBusy):
        enqueue(domain)
    assert VerificationAction.objects.count() == 500


def test_contact_edit_during_provider_lookup_prevents_resend(domain):
    job = enqueue(domain, "resend")
    client = provider(domain)
    payload = client.get_registrant_verification_details.return_value
    def edit(*args):
        DomainContact.objects.filter(pk=domain.order.registration_contact_id).update(email="edited@example.com")
        return payload
    client.get_registrant_verification_details.side_effect = edit
    drive(job, client)
    assert job.status == "failed"
    client.resend_registrant_verification.assert_not_called()


def test_http_requires_csrf(domain):
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    client.force_login(domain.user)
    response = client.post(reverse("domains:verification_action", kwargs={"pk": domain.pk}), {
        "kind": "refresh", "key": str(uuid.uuid4()),
    })
    assert response.status_code == 403
    assert not VerificationAction.objects.exists()


def test_cache_failure_returns_generic_unavailable_without_queue(domain, client):
    from apps.core.abuse import AbuseStoreUnavailable

    client.force_login(domain.user)
    # Shared-store failure prevents admission before any provider work.
    with patch("apps.core.abuse.consume_limit", side_effect=AbuseStoreUnavailable):
        response = client.post(reverse("domains:verification_action", kwargs={"pk": domain.pk}), {
            "kind": "refresh", "key": str(uuid.uuid4()),
        })
    assert response.status_code == 503
    assert not VerificationAction.objects.exists()


def test_actor_deactivated_during_lookup_cannot_send_email(domain):
    from apps.accounts.models import User

    job = enqueue(domain, "resend")
    client = provider(domain)
    payload = client.get_registrant_verification_details.return_value
    def deactivate(*args):
        User.objects.filter(pk=domain.user_id).update(is_active=False)
        return payload
    client.get_registrant_verification_details.side_effect = deactivate
    drive(job, client)
    assert job.status == "cancelled"
    client.resend_registrant_verification.assert_not_called()


def test_invalid_http_action_cannot_admit_job(domain, client):
    client.force_login(domain.user)
    response = client.post(reverse("domains:verification_action", kwargs={"pk": domain.pk}), {
        "kind": "change-recipient", "key": "invalid", "email": "attacker@example.com",
    })
    assert response.status_code == 400
    assert not VerificationAction.objects.exists()


def test_resolution_rejects_nonowner_and_does_not_release_uncertain_action(domain):
    from django.core.management.base import CommandError

    job = enqueue(domain, "resend")
    VerificationAction.objects.filter(pk=job.pk).update(status="unknown")
    with pytest.raises(CommandError):
        call_command("resolve_verification_action", action_id=job.pk, owner_id=domain.user_id,
                     outcome="not-sent", reason="Provider confirmed no email was sent", provider_reference="CASE-123", stdout=StringIO())
    job.refresh_from_db()
    assert job.status == "unknown"


def test_dispatch_task_is_registered_and_scheduled():
    from cyberask_domains.celery import app
    from django.conf import settings

    assert "domains.run_verification_action" in app.tasks
    assert "domains.dispatch_verification_actions" in app.tasks
    assert settings.CELERY_BEAT_SCHEDULE["registrar-verification-dispatch"] == {
        "task": "domains.dispatch_verification_actions", "schedule": 60.0,
    }
