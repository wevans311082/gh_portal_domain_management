from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.models import DomainContact, DomainOrder, DomainRegistrationAttempt
from apps.domains.registration_submission import submit_registration, RegistrationInProgress, RegistrationReviewRequired

NS = ["ns1.example.com", "ns2.example.com"]
pytestmark = pytest.mark.django_db


def _new_order(django_user_model):
    user = django_user_model.objects.create_user(email="paid-submission@example.test", password="testpass")
    contact = DomainContact.objects.create(user=user, registrant_type="individual", name="Jane Smith", email=user.email,
        phone_country_code="44", phone="07700900000", address_line1="1 High Street", city="London", state="London", postcode="SW1A 1AA", country="GB")
    return DomainOrder.objects.create(user=user, domain_name="paid-submission.com", tld="com", status="paid",
        registration_contact=contact, admin_contact=contact, tech_contact=contact, billing_contact=contact)


@pytest.fixture
def order(django_user_model):
    return _new_order(django_user_model)


@pytest.fixture
def agreed_order(django_user_model):
    from tests.agreement_helpers import attach_agreement
    return attach_agreement(_new_order(django_user_model))


@pytest.fixture
def provider():
    return Mock(create_contact=Mock(return_value="201"), register_domain=Mock(return_value={"entityid": "501"}))


def submit(order, provider):
    return submit_registration(order.pk, "999", NS, client=provider)


def test_registration_handoff_retains_full_delegation_and_replay_preserves_later_cache(agreed_order, provider, settings):
    from apps.domains.models import Domain
    from apps.domains.tasks import register_domain_order
    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    hosts = [f"ns{i}.example.com" for i in range(1, 14)]
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch(
        "apps.domains.tasks._build_nameservers", return_value=hosts
    ):
        domain_id = register_domain_order.run(agreed_order.pk)
        domain = Domain.objects.get(pk=domain_id)
        assert domain.cached_nameservers == hosts
        assert [getattr(domain, f"nameserver{i}") for i in range(1, 5)] == hosts[:4]
        domain.set_nameserver_cache(["new1.example.com", "new2.example.com"])
        domain.save()
        assert register_domain_order.run(agreed_order.pk) == domain_id
    domain.refresh_from_db()
    assert domain.cached_nameservers == ["new1.example.com", "new2.example.com"]
    assert provider.register_domain.call_count == 1


def test_acknowledged_purchase_is_durable_and_replay_never_calls_registrar_again(agreed_order, provider):
    assert submit(agreed_order, provider) == "501"
    assert submit(agreed_order, provider) == "501"
    attempt = DomainRegistrationAttempt.objects.get(order=agreed_order)
    agreed_order.refresh_from_db()
    assert attempt.status == "accepted" and agreed_order.registrar_order_id == "501"
    assert attempt.role_ids["reg"] == "201" and agreed_order.registrar_contact_ids == attempt.role_ids
    assert provider.create_contact.call_count == provider.register_domain.call_count == 1
    assert agreed_order.user.email not in str(attempt.subject)
    assert "07700900000" not in str(attempt.subject)


def test_acknowledged_order_cannot_be_rebound_to_different_remote_contacts(agreed_order, provider):
    submit(agreed_order, provider)
    DomainOrder.objects.filter(pk=agreed_order.pk).update(registrar_contact_ids={"reg": "9999"})
    with pytest.raises(RegistrationReviewRequired, match="linkage changed"):
        submit(agreed_order, provider)
    assert provider.register_domain.call_count == 1


def test_existing_known_legacy_order_is_retained_without_contact_or_paid_mutations(agreed_order, provider):
    agreed_order.registrar_order_id = "501"
    agreed_order.save(update_fields=["registrar_order_id"])
    with pytest.raises(RegistrationReviewRequired):
        submit(agreed_order, provider)
    with pytest.raises(RegistrationReviewRequired):
        submit(agreed_order, provider)
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()
    attempt = DomainRegistrationAttempt.objects.get()
    assert attempt.legacy_review_required and attempt.status == "unknown"
    assert AuditLog.objects.filter(action="domain.registration.historical_review_required").count() == 1


def test_overlapping_preparation_cannot_reach_another_paid_post(agreed_order, provider):
    def create(payload):
        with pytest.raises(RegistrationInProgress):
            submit(agreed_order, provider)
        return "201"
    provider.create_contact.side_effect = create
    assert submit(agreed_order, provider) == "501"
    assert provider.register_domain.call_count == 1


def test_edit_of_an_earlier_role_while_syncing_last_role_stops_paid_submission(agreed_order, provider):
    last = DomainContact.objects.get(pk=agreed_order.registration_contact_id)
    last.pk = None
    last.label = "Last role"
    last.save()
    agreed_order.billing_contact = last
    agreed_order.save()
    calls = []
    def create(payload):
        calls.append(payload)
        if len(calls) == 2:
            DomainContact.objects.filter(pk=agreed_order.registration_contact_id).update(name="Changed after first sync")
        return str(200 + len(calls))
    provider.create_contact.side_effect = create
    with pytest.raises(ValidationError, match="acceptance"):
        submit(agreed_order, provider)
    assert DomainRegistrationAttempt.objects.get().status == "aborted"
    provider.register_domain.assert_not_called()
    from tests.agreement_helpers import attach_agreement
    attach_agreement(agreed_order)
    assert submit(agreed_order, provider) == "501"
    assert provider.register_domain.call_count == 1


@pytest.mark.parametrize("changes", [{"status": "cancelled"}, {"registration_years": 2}, {"total_price": 100}])
def test_order_changes_during_preparation_stop_the_paid_call(agreed_order, provider, changes):
    def create(payload):
        DomainOrder.objects.filter(pk=agreed_order.pk).update(**changes)
        return "201"
    provider.create_contact.side_effect = create
    with pytest.raises(ValidationError):
        submit(agreed_order, provider)
    provider.register_domain.assert_not_called()
    assert DomainRegistrationAttempt.objects.get().status == "aborted"


def test_provider_timeout_blocks_all_automatic_paid_replays(agreed_order, provider):
    provider.register_domain.side_effect = TimeoutError("PRIVATE RESPONSE AND SECRET")
    with pytest.raises(TimeoutError):
        submit(agreed_order, provider)
    attempt = DomainRegistrationAttempt.objects.get()
    assert attempt.status == "unknown" and attempt.submitted_at
    with pytest.raises(RegistrationReviewRequired):
        submit(agreed_order, provider)
    assert provider.register_domain.call_count == 1
    assert "PRIVATE RESPONSE" not in str(attempt.__dict__)


@pytest.mark.parametrize("response", [True, False, [], {}, {"entityid": True}, {"entityid": "private-body"}, {"entityid": "501", "status": "ERROR"}])
def test_unacknowledged_or_malformed_responses_require_review_without_replay(agreed_order, provider, response):
    provider.register_domain.return_value = response
    with pytest.raises(RegistrationReviewRequired):
        submit(agreed_order, provider)
    assert DomainRegistrationAttempt.objects.get().status == "unknown"
    agreed_order.refresh_from_db()
    assert not agreed_order.registrar_order_id


def test_failed_acknowledgement_audit_preserves_uncertainty_after_provider_success(agreed_order, provider):
    from apps.domains.registration_submission import _audit
    def audit(attempt, action):
        if action == "domain.registration.acknowledged":
            raise DatabaseError("audit unavailable")
        return _audit(attempt, action)
    with patch("apps.domains.registration_submission._audit", side_effect=audit):
        with pytest.raises(DatabaseError):
            submit(agreed_order, provider)
    assert DomainRegistrationAttempt.objects.get().status == "unknown"
    agreed_order.refresh_from_db()
    assert not agreed_order.registrar_order_id
    with pytest.raises(RegistrationReviewRequired):
        submit(agreed_order, provider)
    assert provider.register_domain.call_count == 1


def test_nested_business_transaction_is_rejected_before_any_provider_mutation(agreed_order, provider):
    with transaction.atomic():
        with pytest.raises(RuntimeError, match="durable"):
            submit(agreed_order, provider)
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()


def test_change_between_durable_marker_and_network_call_is_stopped_and_requires_review(agreed_order, provider):
    from apps.domains.registration_submission import _locked_subject
    count = 0
    def subject(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 3:
            DomainContact.objects.filter(pk=agreed_order.registration_contact_id).update(name="Changed before paid call")
        return _locked_subject(*args, **kwargs)
    with patch("apps.domains.registration_submission._locked_subject", side_effect=subject):
        with pytest.raises(ValidationError, match="evidence"):
            submit(agreed_order, provider)
    provider.register_domain.assert_not_called()
    assert DomainRegistrationAttempt.objects.get().status == "unknown"
    with pytest.raises(RegistrationReviewRequired):
        submit(agreed_order, provider)


def test_stale_worker_cannot_submit_after_preparation_is_released(agreed_order, provider, django_user_model):
    owner = django_user_model.objects.create_superuser(email="fence-owner@example.test", password="testpass")
    def create(payload):
        attempt = DomainRegistrationAttempt.objects.get()
        DomainRegistrationAttempt.objects.filter(pk=attempt.pk).update(updated_at=timezone.now() - timedelta(minutes=16))
        call_command("reconcile_registration", attempt_id=attempt.pk, owner_id=owner.pk, action="release-preparation",
                     provider_reference="worker-stopped", reason="Reviewed and stopped the original preparation worker.")
        return "201"
    provider.create_contact.side_effect = create
    with pytest.raises(ValidationError, match="changed"):
        submit(agreed_order, provider)
    provider.register_domain.assert_not_called()
    assert DomainRegistrationAttempt.objects.get().status == "aborted"


def test_worker_never_retries_an_uncertain_paid_call_or_exposes_private_errors(agreed_order, provider, settings, caplog):
    from apps.domains.tasks import register_domain_order
    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    provider.register_domain.side_effect = TimeoutError("PRIVATE-DIAGNOSTIC-AND-SECRET")
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch(
        "apps.domains.tasks._build_nameservers", return_value=NS
    ), patch.object(register_domain_order, "retry") as retry:
        with pytest.raises(RegistrationReviewRequired):
            register_domain_order.run(agreed_order.pk)
        retry.assert_not_called()
    agreed_order.refresh_from_db()
    assert agreed_order.status == "failed" and "PRIVATE-DIAGNOSTIC" not in agreed_order.last_error
    assert "PRIVATE-DIAGNOSTIC" not in caplog.text


def test_dns_failure_keeps_remote_id_and_resumes_without_another_paid_call(agreed_order, provider, settings):
    from apps.domains.tasks import register_domain_order
    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch(
        "apps.domains.tasks._build_nameservers", return_value=NS
    ):
        with patch("apps.domains.tasks.DNSZone.objects.update_or_create", side_effect=DatabaseError("local DNS unavailable")), patch.object(
            register_domain_order, "retry", side_effect=RuntimeError("local retry")
        ):
            with pytest.raises(RuntimeError, match="local retry"):
                register_domain_order.run(agreed_order.pk)
        agreed_order.refresh_from_db()
        assert agreed_order.registrar_order_id == "501" and agreed_order.domain_id
        assert DomainRegistrationAttempt.objects.get().status == "accepted"
        agreed_order.status = "paid"
        agreed_order.save(update_fields=["status"])
        assert register_domain_order.run(agreed_order.pk) == agreed_order.domain_id
    assert provider.register_domain.call_count == provider.create_contact.call_count == 1


def test_local_completion_never_overwrites_a_domain_owned_by_someone_else(agreed_order, provider, settings, django_user_model):
    from apps.domains.models import Domain
    from apps.domains.tasks import register_domain_order
    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    other = django_user_model.objects.create_user(email="foreign-domain-owner@example.test", password="testpass")
    def register(**kwargs):
        Domain.objects.create(user=other, name=agreed_order.domain_name, tld="com", registrar_id="9999")
        return {"entityid": "501"}
    provider.register_domain.side_effect = register
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch("apps.domains.tasks._build_nameservers", return_value=NS):
        with pytest.raises(RegistrationReviewRequired):
            register_domain_order.run(agreed_order.pk)
    assert Domain.objects.get(name=agreed_order.domain_name).user_id == other.pk
    assert DomainRegistrationAttempt.objects.get().status == "accepted"


def test_in_progress_attempt_blocks_staff_edit_delete_cancel_and_retry(agreed_order, provider, client, django_user_model):
    from django.urls import reverse
    from tests.security_helpers import login_owner
    from apps.domains.registration_submission import _claim
    _claim(agreed_order.pk, "999", NS, provider)
    owner = django_user_model.objects.create_user(email="staff-attempt-owner@example.test", password="testpass")
    login_owner(client, owner)
    detail = reverse("admin_tools:domain_order_detail", args=[agreed_order.pk])
    with patch("apps.domains.tasks._build_nameservers", return_value=NS):
        response = client.get(detail)
    assert response.status_code == 200 and b"Preparing contacts" in response.content
    assert b"Process / register" not in response.content
    assert client.post(detail, {"domain_name": "changed.com"}).status_code == 302
    with patch("apps.admin_tools.domain_order_views.register_domain_order.apply") as apply:
        for action in ("cancel", "pause", "delete", "retry_failed", "mark_paid", "process"):
            assert client.post(reverse("admin_tools:domain_order_action", args=[agreed_order.pk, action])).status_code == 302
        apply.assert_not_called()
    agreed_order.refresh_from_db()
    assert agreed_order.status == "paid" and agreed_order.domain_name == "paid-submission.com"
    assert DomainRegistrationAttempt.objects.get().status == "preparing"


def test_release_stale_preparation_is_audited_and_rotates_worker_fence(agreed_order, provider, django_user_model):
    from apps.domains.registration_submission import _claim
    attempt, _ = _claim(agreed_order.pk, "999", NS, provider)
    owner = django_user_model.objects.create_superuser(email="release-owner@example.test", password="testpass")
    options = dict(attempt_id=attempt.pk, owner_id=owner.pk, action="release-preparation", provider_reference="case-123", reason="Confirmed the old preparation worker has stopped.")
    with pytest.raises(CommandError, match="older"):
        call_command("reconcile_registration", **options)
    DomainRegistrationAttempt.objects.filter(pk=attempt.pk).update(updated_at=timezone.now() - timedelta(minutes=16))
    call_command("reconcile_registration", **options)
    previous_token = attempt.token
    attempt.refresh_from_db()
    assert attempt.status == "aborted" and attempt.token != previous_token
    assert AuditLog.objects.filter(action="domain.registration.reconciled", user=owner).exists()
    provider.register_domain.assert_not_called()


def test_reconciliation_reads_and_matches_all_provider_identifiers_before_linking(agreed_order, provider, django_user_model):
    provider.register_domain.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        submit(agreed_order, provider)
    attempt = DomainRegistrationAttempt.objects.get()
    owner = django_user_model.objects.create_superuser(email="link-owner@example.test", password="testpass")
    payload = {"orderid": "501", "customerid": "999", "domainname": agreed_order.domain_name,
               "registrantcontactid": "201", "admincontactid": "201", "techcontactid": "201", "billingcontactid": "201", "domsecret": "NEVER STORE"}
    provider.get_registrant_verification_details.return_value = payload
    options = dict(attempt_id=attempt.pk, owner_id=owner.pk, action="link-order", remote_order_id="501", provider_reference="case-123", reason="Provider support confirmed the original registration order.")
    with patch("apps.domains.management.commands.reconcile_registration.ResellerClubClient", return_value=provider):
        provider.get_registrant_verification_details.return_value = {**payload, "billingcontactid": "202"}
        with pytest.raises(CommandError, match="matched"):
            call_command("reconcile_registration", **options)
        provider.get_registrant_verification_details.return_value = payload
        call_command("reconcile_registration", **options)
    assert submit(agreed_order, provider) == "501"
    assert provider.register_domain.call_count == 1
    attempt.refresh_from_db()
    assert "NEVER STORE" not in str(attempt.__dict__)
