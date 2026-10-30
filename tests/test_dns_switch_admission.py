import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.domains.dns_changes import create_dns_preview, confirm_dns_change
from apps.domains.models import DomainDNSChange, DomainDNSReservation
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_cloudflare_scope import provider

pytestmark = pytest.mark.django_db


def preview(current, **kwargs):
    values = dict(actor=current[0].user, domain_id=current[0].pk, target_provider="cloudflare", key=uuid.uuid4(), client=provider())
    values.update(kwargs)
    return create_dns_preview(**values)


def confirm(change, **kwargs):
    values = dict(actor=change.requested_by, change_id=change.pk, manifest_digest=change.manifest_digest, confirmed=True, client=provider())
    values.update(kwargs)
    return confirm_dns_change(**values)


def test_preview_replay_retains_private_source_without_side_effects(current):
    key = uuid.uuid4()
    with patch("requests.request", side_effect=AssertionError("No external calls")):
        change = preview(current, key=key)
        assert preview(current, key=key).pk == change.pk
    assert change.source["inventory_verified"] is False
    assert not DomainDNSReservation.objects.exists() and DomainDNSChange.objects.count() == 1
    assert current[0].dns_provider == "registrar"


def test_confirmation_reserves_inventory_stage_and_exact_replay_does_not_restart(current):
    change = preview(current)
    assert confirm(change).status == "inventory"
    assert confirm(change).status == "inventory"
    assert DomainDNSReservation.objects.filter(change=change, domain=current[0]).count() == 1
    current[0].refresh_from_db()
    assert current[0].dns_provider == "registrar" and current[0].registrar_nameservers == []


def test_changed_provider_configuration_requires_new_confirmation(current):
    change = preview(current)
    with pytest.raises(ValidationError, match="configuration changed"):
        confirm(change, client=provider(account="b" * 32))
    assert not DomainDNSReservation.objects.exists()


def test_changed_source_requires_new_review(current):
    change = preview(current)
    current[0].cloudflare_zone_id = "changed"
    current[0].save()
    with pytest.raises(ValidationError, match="source or provider"):
        confirm(change)
    assert not DomainDNSReservation.objects.exists()


def test_dns_slot_blocks_bulk_and_local_edits(current):
    from apps.domains.bulk_reservations import assert_local_domain_editable
    from apps.domains.registrar_review import registrar_review_required
    from tests.test_bulk_domain_admission import preview as bulk_preview
    change = preview(current)
    confirm(change)
    assert registrar_review_required(current[0])
    with pytest.raises(ValidationError, match="DNS provider switch"):
        assert_local_domain_editable(current[0])
    batch = bulk_preview(current)
    assert batch.items.get().status == "held"


def test_bulk_slot_blocks_dns_preview(current):
    from tests.test_bulk_domain_admission import preview as bulk_preview, confirm as bulk_confirm
    bulk_confirm(bulk_preview(current))
    with pytest.raises(ValidationError, match="bulk operation"):
        preview(current)


def test_foreign_actor_cannot_preview_or_confirm(current, django_user_model):
    other = django_user_model.objects.create_user(email="foreign-dns@example.test", password="testpass")
    with pytest.raises(PermissionDenied):
        preview(current, actor=other)
    change = preview(current)
    with pytest.raises(PermissionDenied):
        confirm(change, actor=other)


def test_tampered_retained_preview_cannot_confirm(current):
    change = preview(current)
    DomainDNSChange.objects.filter(pk=change.pk).update(source={"tampered": True})
    with pytest.raises(ValidationError, match="preview changed"):
        confirm(change)
    assert not DomainDNSReservation.objects.exists()


def test_confirmation_audit_failure_rolls_back_slot_and_state(current):
    change = preview(current)
    with patch("apps.domains.dns_changes.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            confirm(change)
    change.refresh_from_db()
    assert change.status == "draft" and change.confirmed_at is None
    assert not DomainDNSReservation.objects.exists()


def test_whm_target_retains_captured_scope_without_http(current):
    whm = SimpleNamespace(host="whm.example.test", port=2087, username="root", api_token="PRIVATE-TOKEN",
        base_url="https://whm.example.test:2087/json-api",
        session=SimpleNamespace(verify=True, headers={"Authorization": "whm root:PRIVATE-TOKEN"}))
    change = preview(current, target_provider="cpanel", client=whm)
    assert len(change.provider_scope) == 64 and "PRIVATE" not in change.provider_scope
    assert confirm(change, client=whm).status == "inventory"


def test_preview_identity_and_confirmation_are_typed(current):
    change = preview(current)
    with pytest.raises(ValidationError):
        preview(current, key=change.key, target_provider="cpanel")
    with pytest.raises(ValidationError):
        confirm(change, confirmed="true")
    with pytest.raises(ValidationError):
        change.target_provider = "cpanel"
        change.save()


def test_expired_preview_cannot_take_domain_slot(current):
    from django.utils import timezone
    from datetime import timedelta
    with patch("apps.domains.dns_changes.timezone.now", return_value=timezone.now() - timedelta(hours=1)):
        change = preview(current)
    with pytest.raises(ValidationError, match="expired"):
        confirm(change)
    assert not DomainDNSReservation.objects.exists()


def test_active_platform_owner_can_review_on_behalf_of_customer(current, django_user_model):
    owner = django_user_model.objects.create_user(email="dns-owner@example.test", password="testpass", is_staff=True, is_superuser=True)
    change = preview(current, actor=owner)
    assert change.user_id == current[0].user_id and change.requested_by_id == owner.pk
    assert confirm(change).status == "inventory"


def test_cached_record_change_requires_new_preview(current):
    from apps.dns.models import DNSZone, DNSRecord
    zone = DNSZone.objects.create(domain=current[0], provider="registrar")
    record = DNSRecord.objects.create(zone=zone, record_type="MX", name="@", content="mail.example.test", priority=10)
    change = preview(current)
    assert change.source["cached_records"][0]["priority"] == 10 and change.source["inventory_verified"] is False
    record.content = "other.example.test"
    record.save()
    with pytest.raises(ValidationError, match="source or provider"):
        confirm(change)


def test_dns_slot_blocks_standalone_contact_admission(current):
    from tests.test_contact_change_admission import request
    confirm(preview(current))
    with pytest.raises(ValidationError):
        request(current)


def test_preview_audit_failure_does_not_leave_request(current):
    with patch("apps.domains.dns_changes.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            preview(current)
    assert not DomainDNSChange.objects.exists()


@pytest.mark.parametrize("field,value", [("zone_submitted_at", None), ("zone_id", "replacement")])
def test_retained_submission_marker_and_zone_association_cannot_be_reset(current, field, value):
    from django.utils import timezone
    change = preview(current)
    change.zone_submitted_at, change.zone_id = timezone.now(), "retained-zone"
    change.save()
    setattr(change, field, value)
    with pytest.raises(ValidationError, match="Retain"):
        change.save()
