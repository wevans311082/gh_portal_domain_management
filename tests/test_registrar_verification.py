from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from apps.domains.models import ContactVerification, Domain, DomainContact, DomainOrder
from apps.domains.verification import reconcile_registrant_verification, registrar_email_summary


def test_provider_read_requests_documented_identity_and_status_options():
    from apps.domains.resellerclub_client import ResellerClubClient

    client = object.__new__(ResellerClubClient)
    client._get = Mock(return_value={"orders.orderid": "456"})
    result = client.get_registrant_verification_details("456")
    client._get.assert_called_once_with("domains/details", {
        "order-id": "456",
        "options": ["OrderDetails", "ContactIds", "RegistrantContactDetails", "DomainStatus"],
    })
    assert result["orderid"] == "456"


@pytest.fixture
def order(db, django_user_model):
    user = django_user_model.objects.create_user(email="verify@example.com", password="testpass")
    contact = DomainContact.objects.create(
        user=user, registrant_type="individual", label="Home", name="Jane Smith",
        email=user.email, registrar_contact_id="123",
    )
    return DomainOrder.objects.create(
        user=user, domain_name="example.com", tld="com", registrar_order_id="456",
        registration_contact=contact, admin_contact=contact, tech_contact=contact, billing_contact=contact,
    )


def provider(order, **changes):
    payload = {
        "orderid": "456", "domainname": "example.com", "customerid": "999",
        "registrantcontactid": "123", "registrantcontact": {"email": order.user.email},
        "raaVerificationStatus": "Verified", "domsecret": "DO-NOT-STORE",
    }
    payload.update(changes)
    return Mock(get_registrant_verification_details=Mock(return_value=payload))


def test_order_contact_snapshot_overrides_shared_legacy_identifier(order):
    order.registrar_contact_ids = {"reg": "789"}
    order.save(update_fields=["registrar_contact_ids"])
    result = reconcile_registrant_verification(
        order.pk, "999", client=provider(order, registrantcontactid="789"),
    )
    assert result.remote_contact_id == "789"
    assert result.is_current
    order.registrar_contact_ids = {"reg": "790"}
    order.save(update_fields=["registrar_contact_ids"])
    assert not result.is_current


def test_order_snapshot_never_falls_back_to_legacy_id_when_registrant_missing(order):
    order.registrar_contact_ids = {"admin": "789"}
    order.save(update_fields=["registrar_contact_ids"])
    client = provider(order)
    with pytest.raises(ValidationError):
        reconcile_registrant_verification(order.pk, "999", client=client)
    client.get_registrant_verification_details.assert_not_called()


def test_order_from_another_provider_environment_is_rejected_before_lookup(order):
    order.registrar_contact_ids = {"reg": "123", "_scope": "different-provider-scope"}
    order.save(update_fields=["registrar_contact_ids"])
    client = provider(order)
    with pytest.raises(ValidationError, match="environment"):
        reconcile_registrant_verification(order.pk, "999", client=client)
    client.get_registrant_verification_details.assert_not_called()


def test_changed_order_binding_during_lookup_cannot_acquire_evidence(order):
    order.registrar_contact_ids = {"reg": "123"}
    order.save(update_fields=["registrar_contact_ids"])
    client = provider(order)
    payload = client.get_registrant_verification_details.return_value

    def read(identifier):
        DomainOrder.objects.filter(pk=order.pk).update(registrar_contact_ids={"reg": "789"})
        return payload

    client.get_registrant_verification_details.side_effect = read
    with pytest.raises(ValidationError, match="changed"):
        reconcile_registrant_verification(order.pk, "999", client=client)
    assert not ContactVerification.objects.exists()


@pytest.mark.parametrize("remote,status", [
    ("Verified", "verified"), ("Pending", "pending"), ("Suspended", "suspended"),
    ("unexpected", "unknown"), (None, "unknown"), ({"status": "Verified"}, "unknown"),
])
def test_provider_status_is_explicit_and_subject_bound(order, remote, status):
    result = reconcile_registrant_verification(order.pk, "999", client=provider(order, raaVerificationStatus=remote))
    assert result.status == status
    assert result.is_current
    assert result.is_verified is (status == "verified")
    assert result.check_kind == "registrar_email"
    assert result.source == "resellerclub"
    assert result.reference == "456"
    assert result.expires_at == result.checked_at + timedelta(minutes=15)
    assert "DO-NOT-STORE" not in str(result.__dict__)
    order.registration_contact.refresh_from_db()
    assert order.registration_contact.registrant_validation_status == "unvalidated"


@pytest.mark.parametrize("changes", [
    {"orderid": "999"}, {"domainname": "other.com"}, {"customerid": "888"},
    {"registrantcontactid": "124"}, {"registrantcontact": {"email": "other@example.com"}},
    {"registrantcontact": {}}, {"registrantcontact": None},
    {"orderid": True}, {"orderid": 456.0},
])
def test_mismatched_provider_subject_cannot_create_evidence(order, changes):
    with pytest.raises(ValidationError):
        reconcile_registrant_verification(order.pk, "999", client=provider(order, **changes))
    assert not ContactVerification.objects.exists()


@pytest.mark.parametrize("field,value", [
    ("email", "changed@example.com"), ("name", "Changed Name"),
    ("registrar_contact_id", "124"), ("registrant_type", "organisation"),
])
def test_intervening_contact_edit_does_not_receive_stale_evidence(order, field, value):
    client = provider(order)
    payload = client.get_registrant_verification_details.return_value
    def mutate(*args):
        DomainContact.objects.filter(pk=order.registration_contact_id).update(**{field: value})
        return payload
    client.get_registrant_verification_details.side_effect = mutate
    with pytest.raises(ValidationError, match="changed"):
        reconcile_registrant_verification(order.pk, "999", client=client)
    assert not ContactVerification.objects.exists()


def test_pending_result_supersedes_previous_verified_result(order):
    old = reconcile_registrant_verification(order.pk, "999", client=provider(order))
    assert old.is_verified
    new = reconcile_registrant_verification(order.pk, "999", client=provider(order, raaVerificationStatus="Pending"))
    assert new.is_current
    assert not old.is_current
    assert not old.is_verified
    assert ContactVerification.objects.count() == 2


@pytest.mark.parametrize("field,value", [
    ("domain_name", "changed.com"), ("tld", "net"), ("registrar_order_id", "777"),
])
def test_subsequent_order_edit_invalidates_domain_evidence(order, field, value):
    result = reconcile_registrant_verification(order.pk, "999", client=provider(order))
    assert result.is_verified
    DomainOrder.objects.filter(pk=order.pk).update(**{field: value})
    assert not result.is_verified


def test_deleted_order_retains_history_without_current_verification(order):
    result = reconcile_registrant_verification(order.pk, "999", client=provider(order))
    order.delete()
    result.refresh_from_db()
    assert result.domain_order_id is None
    assert not result.is_current


def test_cc_tld_does_not_receive_universal_fifteen_day_deadline(order):
    order.domain_name = "example.io"
    order.tld = "io"
    order.save()
    result = reconcile_registrant_verification(order.pk, "999", client=provider(
        order, domainname="example.io", raaVerificationStatus="Pending",
        raaVerificationStartTime=str(int(timezone.now().timestamp()) - 3600),
    ))
    assert result.provider_started_at is not None
    assert result.action_due_at is None


def test_provider_failure_is_not_replaced_with_local_approval(order):
    client = Mock()
    client.get_registrant_verification_details.side_effect = TimeoutError("private diagnostic")
    with pytest.raises(TimeoutError):
        reconcile_registrant_verification(order.pk, "999", client=client)
    assert not ContactVerification.objects.exists()


def test_unsaved_result_is_not_current_evidence(order):
    result = ContactVerification(
        contact=order.registration_contact, check_kind="identity", source="test",
        subject_digest=ContactVerification.digest_for(order.registration_contact),
        checked_at=timezone.now(), status="verified",
    )
    assert not result.is_current
    assert not result.is_verified


def test_pending_gtld_has_bounded_raa_deadline(order):
    start = timezone.now() - timedelta(days=1)
    result = reconcile_registrant_verification(order.pk, "999", client=provider(
        order, raaVerificationStatus="Pending", raaVerificationStartTime=str(int(start.timestamp())),
    ))
    assert result.action_due_at == result.provider_started_at + timedelta(days=15)


@pytest.mark.parametrize("start", [None, "invalid", "9999999999999999999", True])
def test_malformed_provider_start_has_no_invented_deadline(order, start):
    result = reconcile_registrant_verification(order.pk, "999", client=provider(
        order, raaVerificationStatus="Pending", raaVerificationStartTime=start,
    ))
    assert result.action_due_at is None


def test_command_uses_provider_read_without_printing_contact_details(order):
    output = StringIO()
    with patch("apps.domains.management.commands.reconcile_registrant_verification.ResellerClubClient", return_value=provider(order)), patch(
        "apps.domains.management.commands.reconcile_registrant_verification.get_runtime_setting", return_value="999"
    ):
        call_command("reconcile_registrant_verification", order_id=order.pk, stdout=output)
    assert "registrar email verified" in output.getvalue()
    assert order.user.email not in output.getvalue()
    assert "DO-NOT-STORE" not in output.getvalue()


def test_summary_does_not_fall_back_to_old_verified_evidence(order):
    domain = Domain.objects.create(user=order.user, name=order.domain_name, tld=order.tld, registrar_id=order.registrar_order_id)
    order.domain = domain
    order.save()
    reconcile_registrant_verification(order.pk, "999", client=provider(order))
    assert registrar_email_summary(domain)["status"] == "verified"
    reconcile_registrant_verification(order.pk, "999", client=provider(order, raaVerificationStatus="Pending"))
    assert registrar_email_summary(domain)["status"] == "pending"
    DomainContact.objects.filter(pk=order.registration_contact_id).update(email="edited@example.com")
    assert registrar_email_summary(domain)["status"] == "unknown"
    assert registrar_email_summary(domain)["label"] == "Needs recheck"


def test_domain_summary_requires_matching_owner(order, django_user_model):
    other = django_user_model.objects.create_user(email="different-owner@example.com", password="testpass")
    domain = Domain.objects.create(user=other, name=order.domain_name, tld=order.tld, registrar_id=order.registrar_order_id)
    order.domain = domain
    order.save()
    reconcile_registrant_verification(order.pk, "999", client=provider(order))
    assert registrar_email_summary(domain)["status"] == "unknown"


def test_customer_page_displays_email_check_without_provider_lookup(order, client):
    from django.urls import reverse

    domain = Domain.objects.create(user=order.user, name=order.domain_name, tld=order.tld, registrar_id=order.registrar_order_id)
    order.domain = domain
    order.save()
    reconcile_registrant_verification(order.pk, "999", client=provider(order))
    client.force_login(order.user)
    with patch("apps.provisioning.whm_client.WHMClient") as whm, patch(
        "apps.domains.resellerclub_client.ResellerClubClient"
    ) as registrar:
        whm.return_value.get_nameservers.return_value = []
        response = client.get(reverse("domains:detail", kwargs={"pk": domain.pk}))
    assert response.status_code == 200
    assert response.context["registrar_email"]["status"] == "verified"
    assert b"Registrar email verification" in response.content
    assert b"Personal identity, business checks and domain eligibility are separate" in response.content
    assert b"DO-NOT-STORE" not in response.content
    registrar.assert_not_called()
