from unittest.mock import patch
import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from apps.audit.models import AuditLog
from apps.core.runtime_settings import get_runtime_setting
from apps.domains.bulk_remote import execute_remote_lock
from apps.domains.models import BulkDomainItem, DomainOperationReservation
from tests.test_bulk_domain_admission import preview, confirm
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db(transaction=True)


class Provider:
    def __init__(self, item, *, result=None, error=None, hook=None):
        self.base_url = get_runtime_setting("RESELLERCLUB_API_URL", "https://httpapi.com/api")
        self.reseller_id = get_runtime_setting("RESELLERCLUB_RESELLER_ID", "")
        self.item = item
        self.result = {"entityid": "501", "eaqid": "812", "actionstatus": "UNDOCUMENTED", "message": "SECRET-PROVIDER-BODY"} if result is None else result
        self.error, self.hook, self.calls = error, hook, []

    def _send(self, kind, order_id):
        assert not connection.in_atomic_block
        retained = BulkDomainItem.objects.get(pk=self.item.pk)
        assert retained.status == "submitting" and retained.submitted_at is not None
        assert DomainOperationReservation.objects.filter(item=retained).exists()
        self.calls.append((kind, order_id))
        if self.hook:
            self.hook()
        if self.error:
            raise self.error
        return self.result

    def lock_domain(self, order_id):
        return self._send("lock", order_id)

    def unlock_domain(self, order_id):
        return self._send("unlock", order_id)


def admitted(current, enabled=False):
    batch = preview(current, target={"enabled": enabled})
    confirm(batch)
    return batch.items.get()


@pytest.mark.parametrize("enabled", [True, False])
def test_lock_submission_commits_marker_before_one_provider_call_and_retains_ack(current, enabled):
    item = admitted(current, enabled)
    provider = Provider(item)
    result = execute_remote_lock(item.pk, client=provider)
    assert result.status == "awaiting" and result.provider_action_id == "812"
    assert execute_remote_lock(item.pk, client=provider).status == "awaiting"
    assert provider.calls == [("lock" if enabled else "unlock", "501")]
    assert DomainOperationReservation.objects.filter(item=item).exists()
    current[0].refresh_from_db()
    assert current[0].is_locked is True  # No unconfirmed local change.
    assert "SECRET-PROVIDER-BODY" not in result.message
    assert "SECRET-PROVIDER-BODY" not in str(list(AuditLog.objects.values_list("data", flat=True)))


def test_timeout_retains_unknown_and_does_not_resubmit(current):
    item = admitted(current)
    provider = Provider(item, error=TimeoutError("SECRET-TRANSPORT"))
    result = execute_remote_lock(item.pk, client=provider)
    assert result.status == "unknown" and result.submitted_at is not None
    execute_remote_lock(item.pk, client=provider)
    assert len(provider.calls) == 1 and DomainOperationReservation.objects.filter(item=item).exists()
    assert "SECRET-TRANSPORT" not in result.message


@pytest.mark.parametrize("result", [{}, {"entityid": "999", "eaqid": "812"}, {"entityid": "501", "eaqid": True}, {"entityid": "501", "eaqid": "812", "status": "ERROR"}, {"entityid": "501", "eaqid": ""}])
def test_malformed_or_error_acknowledgements_are_uncertain(current, result):
    item = admitted(current)
    provider = Provider(item, result=result)
    assert execute_remote_lock(item.pk, client=provider).status == "unknown"
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_duplicate_delivery_during_provider_call_does_not_send_again(current):
    item = admitted(current)
    provider = Provider(item)
    provider.hook = lambda: execute_remote_lock(item.pk, client=provider)
    assert execute_remote_lock(item.pk, client=provider).status == "awaiting"
    assert len(provider.calls) == 1


def test_late_ack_can_update_its_own_uncertain_marker_without_a_new_post(current):
    item = admitted(current)
    provider = Provider(item, hook=lambda: BulkDomainItem.objects.filter(pk=item.pk).update(status="unknown"))
    result = execute_remote_lock(item.pk, client=provider)
    assert result.status == "awaiting" and result.provider_action_id == "812"
    assert len(provider.calls) == 1


def test_wrong_provider_account_fails_before_marker_and_http(current):
    item = admitted(current)
    provider = Provider(item)
    provider.reseller_id = "different-account"
    result = execute_remote_lock(item.pk, client=provider)
    assert result.status == "failed" and result.submitted_at is None
    assert not provider.calls and not DomainOperationReservation.objects.filter(item=item).exists()


def test_crash_after_durable_marker_never_repeats_post(current):
    item = admitted(current)
    provider = Provider(item, error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        execute_remote_lock(item.pk, client=provider)
    item.refresh_from_db()
    assert item.status == "submitting" and item.submitted_at is not None
    execute_remote_lock(item.pk, client=provider)
    assert len(provider.calls) == 1 and DomainOperationReservation.objects.filter(item=item).exists()


def test_marker_audit_failure_prevents_http_and_retains_queued_request(current):
    item = admitted(current)
    provider = Provider(item)
    with patch("apps.domains.bulk_remote.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            execute_remote_lock(item.pk, client=provider)
    item.refresh_from_db()
    assert item.status == "queued" and item.submitted_at is None and not provider.calls


def test_result_audit_failure_retains_durable_marker_and_does_not_repeat_http(current):
    item = admitted(current)
    provider = Provider(item)
    real_create = AuditLog.objects.create
    def audit(**kwargs):
        if kwargs["action"] == "domain.bulk.submission_observed":
            raise RuntimeError("audit unavailable")
        return real_create(**kwargs)
    with patch("apps.domains.bulk_remote.AuditLog.objects.create", side_effect=audit):
        with pytest.raises(RuntimeError):
            execute_remote_lock(item.pk, client=provider)
    item.refresh_from_db()
    assert item.status == "submitting" and item.submitted_at is not None
    execute_remote_lock(item.pk, client=provider)
    assert len(provider.calls) == 1


def test_remote_execution_refuses_an_enclosing_transaction(current):
    item = admitted(current)
    provider = Provider(item)
    with transaction.atomic():
        with pytest.raises(ValidationError, match="outside an enclosing"):
            execute_remote_lock(item.pk, client=provider)
    assert not provider.calls
