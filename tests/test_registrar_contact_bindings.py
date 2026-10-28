from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.domains.models import DomainContact, RegistrarContactBinding
from apps.domains.services import DomainContactService


@pytest.fixture
def contact(db, django_user_model):
    user = django_user_model.objects.create_user(email="immutable@example.com", password="testpass")
    return DomainContact.objects.create(
        user=user, registrant_type="individual", name="Jane Smith", email=user.email,
        phone_country_code="44", phone="07700900000", address_line1="1 High Street",
        city="London", state="London", postcode="SW1A 1AA", country="GB",
        registrar_contact_id="123",
    )


def test_type_and_customer_scoped_bindings_never_modify_legacy_contact(contact):
    client = Mock(create_contact=Mock(side_effect=["201", "202", "203"]))
    service = DomainContactService(client)
    assert service.sync_remote_contact(contact, "999", "com") == "201"
    assert service.sync_remote_contact(contact, "999", "co.uk") == "202"
    assert service.sync_remote_contact(contact, "999", "net") == "201"
    assert service.sync_remote_contact(contact, "888", "com") == "203"
    assert client.create_contact.call_count == 3
    client.update_contact.assert_not_called()
    contact.refresh_from_db()
    assert contact.registrar_contact_id == "123"
    assert set(RegistrarContactBinding.objects.values_list("contact_type", flat=True)) == {"Contact", "UkContact"}


def test_edit_creates_new_binding_and_restoring_details_reuses_original(contact):
    client = Mock(create_contact=Mock(side_effect=["201", "202"]))
    service = DomainContactService(client)
    assert service.sync_remote_contact(contact, "999", "com") == "201"
    contact.name = "Jane Newname"
    contact.save()
    assert service.sync_remote_contact(contact, "999", "com") == "202"
    contact.name = "Jane Smith"
    contact.save()
    assert service.sync_remote_contact(contact, "999", "com") == "201"
    client.update_contact.assert_not_called()


def test_provider_environment_and_reseller_changes_do_not_reuse_bindings(contact):
    client = Mock(base_url="https://test.httpapi.com/api", reseller_id="100")
    client.create_contact.side_effect = ["201", "202", "203"]
    service = DomainContactService(client)
    assert service.sync_remote_contact(contact, "999", "com") == "201"
    client.base_url = "https://httpapi.com/api"
    assert service.sync_remote_contact(contact, "999", "com") == "202"
    client.reseller_id = "101"
    assert service.sync_remote_contact(contact, "999", "com") == "203"
    assert RegistrarContactBinding.objects.values("provider_scope").distinct().count() == 3


def test_timeout_reservation_survives_and_prevents_automatic_duplicate(contact):
    client = Mock(create_contact=Mock(side_effect=TimeoutError("private provider diagnostic")))
    service = DomainContactService(client)
    with pytest.raises(TimeoutError):
        service.sync_remote_contact(contact, "999", "com")
    assert RegistrarContactBinding.objects.get().status == "unknown"
    with pytest.raises(ValueError, match="reconcile"):
        service.sync_remote_contact(contact, "999", "com")
    assert client.create_contact.call_count == 1
    assert "private provider diagnostic" not in str(RegistrarContactBinding.objects.get().__dict__)


def test_inflight_and_crashed_creation_never_starts_another_post(contact):
    client = Mock()
    service = DomainContactService(client)

    def create(payload):
        assert RegistrarContactBinding.objects.get().status == "creating"
        with pytest.raises(ValueError, match="pending"):
            service.sync_remote_contact(contact, "999", "com")
        return "201"

    client.create_contact.side_effect = create
    assert service.sync_remote_contact(contact, "999", "com") == "201"
    assert client.create_contact.call_count == 1


def test_midflight_edit_keeps_remote_history_but_does_not_attach_stale_contact(contact):
    client = Mock()

    def create(payload):
        DomainContact.objects.filter(pk=contact.pk).update(name="Changed during request")
        return "201"

    client.create_contact.side_effect = create
    with pytest.raises(ValidationError, match="changed"):
        DomainContactService(client).sync_remote_contact(contact, "999", "com")
    binding = RegistrarContactBinding.objects.get()
    assert (binding.status, binding.remote_id) == ("ready", "201")


def test_nested_business_transaction_is_rejected_before_provider_mutation(contact):
    client = Mock()
    with transaction.atomic():
        with pytest.raises(RuntimeError, match="durable"):
            DomainContactService(client).sync_remote_contact(contact, "999", "com")
    client.create_contact.assert_not_called()
    assert not RegistrarContactBinding.objects.exists()


def test_deprecated_endpoint_is_disabled_even_for_direct_callers():
    from apps.domains.resellerclub_client import ResellerClubClient

    client = object.__new__(ResellerClubClient)
    client._post = Mock()
    with pytest.raises(ValueError, match="immutable"):
        client.update_contact("123", {})
    client._post.assert_not_called()


def test_reconciliation_is_audited_and_ready_binding_cannot_be_overwritten(contact, django_user_model):
    from django.core.management import call_command, CommandError
    from apps.audit.models import AuditLog

    owner = django_user_model.objects.create_superuser(email="owner-binding@example.com", password="testpass")
    client = Mock(create_contact=Mock(side_effect=TimeoutError()))
    with pytest.raises(TimeoutError):
        DomainContactService(client).sync_remote_contact(contact, "999", "com")
    binding = RegistrarContactBinding.objects.get()
    options = dict(
        binding_id=binding.pk, owner_id=owner.pk, remote_id="789",
        confirmed_payload_digest=binding.payload_digest, provider_reference="case-123",
        reason="Provider support confirmed the matching immutable contact.",
    )
    with pytest.raises(CommandError, match="digest"):
        call_command("resolve_registrar_contact", **{**options, "confirmed_payload_digest": "wrong"})
    call_command("resolve_registrar_contact", **options)
    assert DomainContactService(client).sync_remote_contact(contact, "999", "com") == "789"
    assert client.create_contact.call_count == 1
    assert AuditLog.objects.filter(action="domain.contact.reconciled", user=owner).count() == 1
    with pytest.raises(CommandError, match="ready"):
        call_command("resolve_registrar_contact", **options)


def test_recent_inflight_binding_cannot_be_reconciled(contact, django_user_model):
    from django.core.management import call_command, CommandError
    from apps.domains.contact_bindings import _fingerprint

    owner = django_user_model.objects.create_superuser(email="recent-owner@example.com", password="testpass")
    binding = RegistrarContactBinding.objects.create(
        contact=contact, customer_id="999", contact_type="Contact",
        payload_digest=_fingerprint(contact.as_resellerclub_payload("999", "com")),
    )
    with pytest.raises(CommandError, match="running"):
        call_command(
            "resolve_registrar_contact", binding_id=binding.pk, owner_id=owner.pk, remote_id="789",
            confirmed_payload_digest=binding.payload_digest, provider_reference="case-123",
            reason="Provider support reviewed the matching contact details.",
        )
