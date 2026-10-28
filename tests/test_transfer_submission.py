from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction

from apps.audit.models import AuditLog
from apps.billing.models import Invoice
from apps.domains.models import Domain, DomainContact, DomainTransfer, DomainTransferAttempt
from apps.domains.tasks import execute_domain_transfer
from apps.domains.transfer_submission import submit_transfer, TransferInProgress, TransferReviewRequired

NS = ["ns1.example.com", "ns2.example.com"]
pytestmark = pytest.mark.django_db


@pytest.fixture
def transfer(django_user_model):
    user = django_user_model.objects.create_user(email="transfer-safety@example.test", password="testpass")
    contact = DomainContact.objects.create(user=user, registrant_type="individual", name="Jane Smith", email=user.email,
        phone_country_code="44", phone="07700900000", address_line1="1 High Street", city="London", state="London", postcode="SW1A 1AA", country="GB")
    invoice = Invoice.objects.create(user=user, number="TRANSFER-SAFETY", status="paid", total=12, amount_paid=12)
    return DomainTransfer.objects.create(user=user, domain_name="safe-transfer.com", tld="com", status="paid",
        invoice=invoice, auth_code="PRIVATE-AUTH-CODE", registration_contact=contact, admin_contact=contact,
        tech_contact=contact, billing_contact=contact, quoted_price=12, total_price=12)


@pytest.fixture
def provider():
    return Mock(create_contact=Mock(return_value="201"), transfer_domain=Mock(return_value={"entityid": "501", "status": "NoError"}))


def submit(transfer, provider):
    return submit_transfer(transfer.pk, "999", NS, client=provider)


def drive(transfer, provider, settings):
    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch("apps.domains.tasks._build_nameservers", return_value=NS):
        return execute_domain_transfer.run(transfer.pk)


def test_acknowledgement_is_retained_and_replay_uses_frozen_contacts_and_nameservers(transfer, provider):
    assert submit(transfer, provider) == "501"
    assert submit_transfer(transfer.pk, "999", ["changed1.example.com", "changed2.example.com"], client=provider) == "501"
    transfer.refresh_from_db()
    attempt = transfer.submission_attempt
    assert attempt.status == "accepted" and transfer.registrar_order_id == "501"
    assert attempt.role_ids == transfer.registrar_contact_ids
    assert attempt.subject["nameservers"] == NS
    assert provider.transfer_domain.call_count == provider.create_contact.call_count == 1
    assert "PRIVATE-AUTH-CODE" not in str(attempt.subject)
    assert transfer.user.email not in str(attempt.subject)
    assert "07700900000" not in str(attempt.subject)


def test_concurrent_worker_is_refused_before_another_contact_or_paid_post(transfer, provider):
    def create(payload):
        with pytest.raises(TransferInProgress):
            submit(transfer, provider)
        return "201"
    provider.create_contact.side_effect = create
    submit(transfer, provider)
    assert provider.transfer_domain.call_count == 1


def test_contact_edit_during_preparation_aborts_without_purchase(transfer, provider):
    def create(payload):
        DomainContact.objects.filter(pk=transfer.registration_contact_id).update(city="Oxford")
        return "201"
    provider.create_contact.side_effect = create
    with pytest.raises((ValueError, ValidationError)):
        submit(transfer, provider)
    provider.transfer_domain.assert_not_called()
    assert transfer.submission_attempt.status == "aborted"


@pytest.mark.parametrize("field,value", [("auth_code", "DIFFERENT"), ("total_price", 99), ("auto_renew", False), ("status", "cancelled")])
def test_changed_subject_during_contact_sync_prevents_paid_post(transfer, provider, field, value):
    def create(payload):
        DomainTransfer.objects.filter(pk=transfer.pk).update(**{field: value})
        return "201"
    provider.create_contact.side_effect = create
    with pytest.raises(ValidationError):
        submit(transfer, provider)
    provider.transfer_domain.assert_not_called()
    assert transfer.submission_attempt.status == "aborted"


@pytest.mark.parametrize("response", [True, {}, {"entityid": True}, {"entityid": "0"}, {"entityid": "501", "status": "ERROR"}, {"entityid": "501", "actionstatus": "Failed"}])
def test_uncertain_acknowledgement_cannot_be_repeated(transfer, provider, response):
    provider.transfer_domain.return_value = response
    with pytest.raises(TransferReviewRequired):
        submit(transfer, provider)
    assert transfer.submission_attempt.status == "unknown"
    with pytest.raises(TransferReviewRequired):
        submit(transfer, provider)
    assert provider.transfer_domain.call_count == 1


def test_network_timeout_blocks_retry_and_raw_diagnostics_are_not_persisted(transfer, provider, settings, caplog):
    provider.transfer_domain.side_effect = RuntimeError("PRIVATE-AUTH-CODE api-key=DO-NOT-STORE")
    drive(transfer, provider, settings)
    drive(transfer, provider, settings)
    transfer.refresh_from_db()
    assert transfer.submission_attempt.status == "unknown" and provider.transfer_domain.call_count == 1
    assert "DO-NOT-STORE" not in transfer.last_error + caplog.text + str(list(AuditLog.objects.values_list("data", flat=True)))


def test_acknowledgement_audit_rollback_keeps_the_committed_paid_marker(transfer, provider):
    original = AuditLog.objects.create
    def create(**kwargs):
        if kwargs["action"] == "domain.transfer.acknowledged":
            raise DatabaseError("ack commit failed")
        return original(**kwargs)
    with patch.object(AuditLog.objects, "create", side_effect=create), pytest.raises(DatabaseError):
        submit(transfer, provider)
    transfer.refresh_from_db()
    assert not transfer.registrar_order_id and transfer.submission_attempt.status == "unknown"
    with pytest.raises(TransferReviewRequired):
        submit(transfer, provider)
    assert provider.transfer_domain.call_count == 1


def test_subject_change_after_marker_does_not_reach_paid_post(transfer, provider):
    from apps.domains import transfer_submission
    original = transfer_submission._audit
    def audit(attempt, action):
        original(attempt, action)
        if action == "submitting":
            DomainTransfer.objects.filter(pk=transfer.pk).update(auth_code="CHANGED-AFTER-MARKER")
    with patch.object(transfer_submission, "_audit", side_effect=audit), pytest.raises(TransferReviewRequired):
        submit(transfer, provider)
    assert transfer.submission_attempt.status == "unknown"
    provider.transfer_domain.assert_not_called()


def test_worker_handoff_retains_pending_domain_and_does_not_claim_registry_completion(transfer, provider, settings):
    pk = drive(transfer, provider, settings)
    transfer.refresh_from_db()
    assert transfer.domain_id == pk and transfer.domain.status == "pending" and transfer.domain.registrar_id == "501"
    assert transfer.status == "processing" and transfer.completed_at is None
    assert drive(transfer, provider, settings) == pk
    assert provider.transfer_domain.call_count == 1


def test_local_handoff_failure_can_resume_without_paid_replay(transfer, provider, settings):
    original = AuditLog.objects.create
    def create(**kwargs):
        if kwargs["action"] == "domain.transfer.local_handoff":
            raise DatabaseError("local commit failed")
        return original(**kwargs)
    with patch.object(AuditLog.objects, "create", side_effect=create):
        drive(transfer, provider, settings)
    transfer.refresh_from_db()
    assert transfer.submission_attempt.status == "accepted" and not transfer.domain_id
    assert not Domain.objects.exists()
    assert drive(transfer, provider, settings)
    assert provider.transfer_domain.call_count == 1


def test_local_domain_cannot_be_reassigned_after_remote_ack(transfer, provider, settings, django_user_model):
    outsider = django_user_model.objects.create_user(email="other-transfer@example.test", password="testpass")
    def paid(**kwargs):
        Domain.objects.create(user=outsider, name=transfer.domain_name, tld="com", registrar_id="777")
        return {"entityid": "501"}
    provider.transfer_domain.side_effect = paid
    drive(transfer, provider, settings)
    domain = Domain.objects.get()
    assert domain.user_id == outsider.pk and domain.registrar_id == "777"
    transfer.refresh_from_db()
    assert not transfer.domain_id and transfer.submission_attempt.status == "accepted"


def test_existing_domain_is_rejected_before_any_contact_mutation(transfer, provider):
    Domain.objects.create(user=transfer.user, name=transfer.domain_name, tld="com")
    with pytest.raises(ValidationError, match="already has a local record"):
        submit(transfer, provider)
    provider.create_contact.assert_not_called()
    provider.transfer_domain.assert_not_called()


def test_pasted_order_identifier_creates_durable_review_instead_of_accepted_state(transfer, provider):
    transfer.registrar_order_id = "501"
    transfer.save()
    with pytest.raises(TransferReviewRequired):
        submit(transfer, provider)
    assert transfer.submission_attempt.legacy_review_required and transfer.submission_attempt.status == "unknown"
    provider.create_contact.assert_not_called()
    provider.transfer_domain.assert_not_called()


def test_invoice_ownership_and_payment_are_checked_before_contacts(transfer, provider, django_user_model):
    outsider = django_user_model.objects.create_user(email="invoice-owner@example.test", password="testpass")
    transfer.invoice.user = outsider
    transfer.invoice.save()
    with pytest.raises(ValidationError, match="paid invoice belonging"):
        submit(transfer, provider)
    provider.create_contact.assert_not_called()


def test_retained_customer_is_used_on_worker_resume_after_default_changes(transfer, provider, settings):
    submit(transfer, provider)
    settings.RESELLERCLUB_CUSTOMER_ID = "12345"
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), patch("apps.domains.tasks._build_nameservers") as nameservers:
        assert execute_domain_transfer.run(transfer.pk)
    nameservers.assert_not_called()
    assert provider.transfer_domain.call_count == 1


def test_nested_business_transaction_cannot_erase_reservation_after_remote_post(transfer, provider):
    with transaction.atomic(), pytest.raises(RuntimeError):
        submit(transfer, provider)
    provider.create_contact.assert_not_called()
    provider.transfer_domain.assert_not_called()


def test_transfer_client_sends_required_invoice_option_without_a_second_customer_invoice():
    from apps.domains.resellerclub_client import ResellerClubClient
    client = object.__new__(ResellerClubClient)
    client._post = Mock(return_value={"entityid": "501"})
    client.transfer_domain("example.com", "999", "1", "2", "3", "4", NS)
    assert client._post.call_args.args[0] == "domains/transfer"
    assert client._post.call_args.args[1]["invoice-option"] == "NoInvoice"


def test_rotated_preparation_fence_prevents_old_worker_reaching_paid_call(transfer, provider):
    import uuid
    replacement = uuid.uuid4()
    def create(payload):
        DomainTransferAttempt.objects.filter(transfer=transfer).update(token=replacement)
        return "201"
    provider.create_contact.side_effect = create
    with pytest.raises(ValidationError, match="details changed"):
        submit(transfer, provider)
    attempt = DomainTransferAttempt.objects.get()
    assert attempt.token == replacement and attempt.status == "preparing"
    provider.transfer_domain.assert_not_called()


def test_registration_and_transfer_records_cannot_purchase_the_same_name(transfer, provider):
    from apps.domains.models import DomainOrder
    from apps.domains.registration_submission import submit_registration
    order = DomainOrder.objects.create(user=transfer.user, domain_name=transfer.domain_name, tld="com", status="paid",
        registration_contact=transfer.registration_contact, admin_contact=transfer.admin_contact,
        tech_contact=transfer.tech_contact, billing_contact=transfer.billing_contact)
    with pytest.raises(ValidationError, match="registration request exists"):
        submit(transfer, provider)
    with pytest.raises(ValidationError, match="transfer request exists"):
        submit_registration(order.pk, "999", NS, client=provider)
    provider.create_contact.assert_not_called()
    provider.register_domain.assert_not_called()
    provider.transfer_domain.assert_not_called()


def test_accepted_transfer_with_changed_provider_scope_cannot_resume(transfer, provider):
    submit(transfer, provider)
    provider.base_url = "https://different-environment.example/api"
    with pytest.raises(TransferReviewRequired, match="association changed"):
        submit(transfer, provider)
    assert provider.transfer_domain.call_count == 1
