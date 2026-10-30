from unittest.mock import patch
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from apps.accounts.models import User
from apps.domains.bulk_preview import normalize_request, preview_bulk
from apps.domains.models import Domain, DomainContactChange, ContactVerification
from apps.domains.registrar_policy import ALL_ROLES
from apps.audit.models import AuditLog
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("action,ids,target", [
    ("transfer", [1], {}), ("lock", [], {"enabled": True}),
    ("lock", [1, 1], {"enabled": True}), ("lock", [True], {"enabled": True}),
    ("lock", list(range(1, 102)), {"enabled": True}),
    ("lock", [1], {"enabled": "true"}), ("lock", [1], {"enabled": True, "epp": True}),
    ("nameservers", [1], {"nameservers": ["NS1.EXAMPLE.TEST.", "ns1.example.test"]}),
    ("nameservers", [1], {"nameservers": ["bad host"]}),
    ("contacts", [1], {"contacts": {"password": 1}, "publication_choice": "redact"}),
])
def test_invalid_or_sensitive_bulk_request_is_rejected(action, ids, target):
    with pytest.raises(ValidationError):
        normalize_request(action, ids, target)


def test_preview_checks_each_item_without_leaking_foreign_domain(current):
    domain, source = current
    foreign = User.objects.create_user(email="bulk-other@example.test", password="test")
    hidden = Domain.objects.create(user=foreign, name="private-foreign.test", tld="test", status="active")
    unsupported = Domain.objects.create(user=source.user, name="unsupported.co.uk", tld="co.uk", status="active", registrar_id="502")
    before = (AuditLog.objects.count(), DomainContactChange.objects.count(), ContactVerification.objects.count())
    with patch("apps.domains.resellerclub_client.ResellerClubClient.__init__", side_effect=AssertionError("Preview must not create an HTTP client")):
        result = preview_bulk(customer=source.user, action="lock", domain_ids=[domain.pk, hidden.pk, unsupported.pk, 987654], target={"enabled": True})
    assert [item.status for item in result["items"]] == ["ready", "unavailable", "held", "unavailable"]
    assert "private-foreign" not in str(result)
    assert result["items"][2].message == "This registry does not support registrar transfer locks."
    assert "provider_scope" not in repr(result["items"][0])
    assert (AuditLog.objects.count(), DomainContactChange.objects.count(), ContactVerification.objects.count()) == before
    domain.refresh_from_db()
    assert domain.is_locked
    assert result["items"][0].status == "ready"  # Cached lock is never provider confirmation.


def test_auto_renew_can_be_disabled_during_registrar_review(current):
    domain, source = current
    domain.auto_renew = True
    domain.save(update_fields=["auto_renew"])
    source.registrar_order_id = "mismatch"
    source.save(update_fields=["registrar_order_id"])
    result = preview_bulk(customer=source.user, action="auto_renew", domain_ids=[domain.pk], target={"enabled": False})
    assert result["items"][0].status == "ready"
    assert result["items"][0].snapshot["local_only"] is True
    held = preview_bulk(customer=source.user, action="auto_renew", domain_ids=[domain.pk], target={"enabled": True})
    assert held["items"][0].status == "held"
    domain.refresh_from_db()
    assert domain.auto_renew is True


def test_nameserver_preview_normalizes_and_respects_profile_bounds(current):
    domain, source = current
    result = preview_bulk(customer=source.user, action="nameservers", domain_ids=[domain.pk], target={"nameservers": [" NS1.EXAMPLE.TEST. ", "ns2.example.test"]})
    assert result["target"]["nameservers"] == ["ns1.example.test", "ns2.example.test"]
    assert result["items"][0].status == "ready"
    held = preview_bulk(customer=source.user, action="nameservers", domain_ids=[domain.pk], target={"nameservers": ["ns1.example.test"]})
    assert held["items"][0].status == "held"


def test_contact_preview_uses_owned_roles_and_does_not_record_consent(current):
    domain, source = current
    target = {"contacts": {role: source.registration_contact_id for role in ALL_ROLES}, "publication_choice": "redact"}
    result = preview_bulk(customer=source.user, action="contacts", domain_ids=[domain.pk], target=target)
    assert result["items"][0].status == "ready"
    assert len(result["items"][0].snapshot["contact_digests"]) == 4
    assert not DomainContactChange.objects.exists()
    target["contacts"]["admin_contact"] = 987654
    held = preview_bulk(customer=source.user, action="contacts", domain_ids=[domain.pk], target=target)
    assert held["items"][0].status == "held"


def test_changed_subject_changes_preview_digest_and_deactivated_owner_is_denied(current):
    domain, source = current
    def preview():
        return preview_bulk(customer=source.user, action="lock", domain_ids=[domain.pk], target={"enabled": False})["items"][0]
    first = preview()
    domain.is_locked = not domain.is_locked
    domain.save(update_fields=["is_locked"])
    assert preview().snapshot["digest"] != first.snapshot["digest"]
    User.objects.filter(pk=source.user_id).update(is_active=False)
    with pytest.raises(PermissionDenied):
        preview()
