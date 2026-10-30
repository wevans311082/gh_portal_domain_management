from datetime import timedelta
from unittest.mock import patch
import pytest
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from apps.core.abuse import AbuseStoreUnavailable
from apps.core.abuse import AbuseLimitExceeded
from apps.domains import bulk_jobs
from apps.domains.bulk_admission import create_bulk_preview, confirm_bulk
from apps.domains.models import BulkDomainItem, Domain, DomainOperationReservation
from tests.test_bulk_domain_remote import admitted, Provider
from tests.test_bulk_domain_nameservers import NameserverProvider
from tests.test_bulk_domain_observation import Reader, evidence
from tests.test_bulk_domain_contacts import admitted_contact
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def isolated_dispatch():
    cache.clear()
    with override_settings(DOMAIN_BULK_EXECUTION_ENABLED=True, CELERY_TASK_ALWAYS_EAGER=False), \
            patch("apps.domains.bulk_jobs.run_bulk_item.delay") as broker, \
            patch("apps.domains.contact_change_jobs.publish_contact_change", return_value=False), \
            patch("apps.domains.bulk_jobs.consume_limit"):
        yield broker
    cache.clear()


def test_default_gate_and_eager_mode_never_publish_or_scan(current, isolated_dispatch):
    item = admitted(current)
    isolated_dispatch.reset_mock()
    with override_settings(DOMAIN_BULK_EXECUTION_ENABLED=False), patch("apps.domains.bulk_jobs._claim", side_effect=AssertionError("No worker claim")):
        bulk_jobs.run_bulk_item(item.pk)
        assert bulk_jobs.dispatch_bulk_items() == 0
        assert bulk_jobs.publish_bulk_item(item.pk) is False
    with override_settings(CELERY_TASK_ALWAYS_EAGER=True):
        assert bulk_jobs.publish_bulk_item(item.pk) is False
        assert bulk_jobs.dispatch_bulk_items() == 0
    assert not isolated_dispatch.called


def test_dispatch_backoff_does_not_make_a_delivered_job_unavailable(current, isolated_dispatch):
    from apps.domains.bulk_remote import execute_remote_lock
    item = admitted(current)
    isolated_dispatch.reset_mock()
    assert bulk_jobs.dispatch_bulk_items() == 1
    assert isolated_dispatch.call_args.args == (item.pk,)
    provider = Provider(item)
    with patch("apps.domains.bulk_remote.execute_remote_lock", side_effect=lambda pk: execute_remote_lock(pk, client=provider)):
        bulk_jobs.run_bulk_item(item.pk)
    item.refresh_from_db()
    assert item.status == "awaiting" and len(provider.calls) == 1
    assert item.dispatch_until is None and item.dispatch_token is not None


def test_duplicate_worker_and_later_observation_never_repeat_mutation(current):
    from apps.domains.bulk_remote import execute_remote_lock
    from apps.domains.bulk_reconciliation import observe_remote_lock
    item = admitted(current)
    provider = Provider(item, hook=lambda: bulk_jobs.run_bulk_item(item.pk))
    with patch("apps.domains.bulk_remote.execute_remote_lock", side_effect=lambda pk: execute_remote_lock(pk, client=provider)):
        bulk_jobs.run_bulk_item(item.pk)
        bulk_jobs.run_bulk_item(item.pk)
    item.refresh_from_db()
    token = item.token
    assert len(provider.calls) == 1
    BulkDomainItem.objects.filter(pk=item.pk).update(available_at=timezone.now() - timedelta(seconds=1))
    reader = Reader(item, evidence(item, []))
    with patch("apps.domains.bulk_reconciliation.observe_remote_lock", side_effect=lambda pk: observe_remote_lock(pk, client=reader)):
        bulk_jobs.run_bulk_item(item.pk)
    item.refresh_from_db()
    assert item.token == token and item.status == "awaiting" and reader.reads == ["501"]
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_local_worker_completes_once_and_contact_worker_links_once(current):
    import uuid
    from apps.audit.models import AuditLog
    customer = current[1].user
    local = Domain.objects.create(user=customer, name="worker-local.test", tld="test", auto_renew=True)
    batch = create_bulk_preview(customer=customer, key=uuid.uuid4(), action="auto_renew", domain_ids=[local.pk], target={"enabled": False})
    confirm_bulk(customer=customer, batch_id=batch.pk, manifest_digest=batch.manifest_digest, confirmed=True)
    bulk_jobs.run_bulk_item(batch.items.get().pk)
    bulk_jobs.run_bulk_item(batch.items.get().pk)
    local.refresh_from_db()
    assert local.auto_renew is False and AuditLog.objects.filter(action="domain.bulk.preference_completed").count() == 1
    contact = admitted_contact(current)
    bulk_jobs.run_bulk_item(contact.pk)
    contact.refresh_from_db()
    assert contact.status == "awaiting" and contact.contact_change_id is not None


def test_shared_cache_failure_defers_without_executor_or_marker(current):
    item = admitted(current)
    with patch("apps.domains.bulk_jobs.consume_limit", side_effect=AbuseStoreUnavailable()), patch("apps.domains.bulk_remote.execute_remote_lock") as execute:
        bulk_jobs.run_bulk_item(item.pk)
    item.refresh_from_db()
    assert item.status == "queued" and item.submitted_at is None and item.dispatch_token is None
    assert item.available_at > timezone.now() and not execute.called


@pytest.mark.parametrize("submitted", [False, True])
def test_worker_errors_do_not_blindly_retry_provider_mutations(current, submitted):
    item = admitted(current)
    def fail(pk):
        if submitted:
            BulkDomainItem.objects.filter(pk=pk).update(status="submitting", submitted_at=timezone.now())
        raise RuntimeError("PRIVATE-WORKER-ERROR")
    with patch("apps.domains.bulk_remote.execute_remote_lock", side_effect=fail):
        bulk_jobs.run_bulk_item(item.pk)
    item.refresh_from_db()
    assert item.status == ("unknown" if submitted else "failed")
    assert DomainOperationReservation.objects.filter(item=item).exists() is submitted
    assert "PRIVATE" not in item.message


def test_old_dispatch_finisher_cannot_clear_a_new_worker_claim(current):
    item = admitted(current)
    first = bulk_jobs._claim(item.pk)
    BulkDomainItem.objects.filter(pk=item.pk).update(dispatch_until=timezone.now() - timedelta(seconds=1))
    second = bulk_jobs._claim(item.pk)
    assert first[0] != second[0]
    bulk_jobs._finish_dispatch(item.pk, first[0])
    item.refresh_from_db()
    assert item.dispatch_token == second[0] and item.dispatch_until > timezone.now()


def test_broker_failure_retains_same_item_for_later_dispatch(current, isolated_dispatch):
    item = admitted(current)
    isolated_dispatch.reset_mock()
    isolated_dispatch.side_effect = RuntimeError("PRIVATE-BROKER-ERROR")
    assert bulk_jobs.dispatch_bulk_items() == 0
    item.refresh_from_db()
    assert item.status == "queued" and item.submitted_at is None
    later = timezone.now() + timedelta(seconds=61)
    isolated_dispatch.side_effect = None
    with patch("apps.domains.bulk_jobs.timezone.now", return_value=later):
        assert bulk_jobs.dispatch_bulk_items() == 1
    assert [call.args for call in isolated_dispatch.call_args_list] == [(item.pk,), (item.pk,)]


def test_interrupted_submission_becomes_observation_only_and_keeps_reservation(current):
    item = admitted(current)
    earlier = timezone.now() - timedelta(minutes=20)
    BulkDomainItem.objects.filter(pk=item.pk).update(status="submitting", submitted_at=earlier, dispatch_until=earlier)
    bulk_jobs.dispatch_bulk_items()
    item.refresh_from_db()
    assert item.status == "unknown" and item.submitted_at == earlier
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_dispatch_is_bounded_and_advances_to_remaining_items(current, isolated_dispatch):
    import uuid
    customer = current[1].user
    domains = Domain.objects.bulk_create([Domain(user=customer, name=f"bounded-{i}.test", tld="test", auto_renew=True) for i in range(120)])
    for selected in (domains[:60], domains[60:]):
        batch = create_bulk_preview(customer=customer, key=uuid.uuid4(), action="auto_renew", domain_ids=[domain.pk for domain in selected], target={"enabled": False})
        confirm_bulk(customer=customer, batch_id=batch.pk, manifest_digest=batch.manifest_digest, confirmed=True)
    isolated_dispatch.reset_mock()
    assert bulk_jobs.dispatch_bulk_items() == 100
    assert isolated_dispatch.call_count == 100
    assert bulk_jobs.dispatch_bulk_items() == 20
    assert isolated_dispatch.call_count == 120
    assert BulkDomainItem.objects.filter(status="queued").count() == 120


def test_nameserver_worker_routes_to_fenced_full_set_submission(current):
    from tests.test_bulk_domain_nameservers import admitted_nameservers
    from apps.domains.bulk_remote import execute_remote_nameservers
    item, values = admitted_nameservers(current)
    provider = NameserverProvider(item)
    with patch("apps.domains.bulk_remote.execute_remote_nameservers", side_effect=lambda pk: execute_remote_nameservers(pk, client=provider)):
        bulk_jobs.run_bulk_item(item.pk)
    item.refresh_from_db()
    assert item.status == "awaiting" and provider.nameservers == values
    assert provider.calls == [("nameservers", "501")]


def test_contact_progress_can_release_confirmed_child_without_a_new_submission(current):
    from apps.domains.models import DomainContactChange
    item = admitted_contact(current)
    bulk_jobs.run_bulk_item(item.pk)
    item.refresh_from_db()
    child_id = item.contact_change_id
    now = timezone.now()
    DomainContactChange.objects.filter(pk=child_id).update(status="confirmed", confirmed_at=now, submitted_at=now)
    BulkDomainItem.objects.filter(pk=item.pk).update(available_at=now - timedelta(seconds=1))
    bulk_jobs.run_bulk_item(item.pk)
    item.refresh_from_db()
    assert item.status == "completed" and item.contact_change_id == child_id
    assert not DomainOperationReservation.objects.filter(item=item).exists()


def test_customer_throttle_does_not_consume_shared_provider_budget(current):
    item = admitted(current)
    def quota(scope, *args):
        if scope == "bulk-domain-worker-customer":
            raise AbuseLimitExceeded(17)
        raise AssertionError("A denied customer must not consume the global budget")
    with patch("apps.domains.bulk_jobs.consume_limit", side_effect=quota), patch("apps.domains.bulk_remote.execute_remote_lock") as execute:
        before = timezone.now()
        bulk_jobs.run_bulk_item(item.pk)
    item.refresh_from_db()
    assert item.available_at >= before + timedelta(seconds=17) and not execute.called
    assert item.status == "queued" and item.submitted_at is None


def test_dispatch_respects_wall_clock_budget_without_dropping_work(current, isolated_dispatch):
    item = admitted(current)
    isolated_dispatch.reset_mock()
    with patch("apps.domains.bulk_jobs.monotonic", side_effect=[100, 109]):
        assert bulk_jobs.dispatch_bulk_items() == 0
    item.refresh_from_db()
    assert item.status == "queued" and not isolated_dispatch.called


def test_live_observation_lease_prevents_interrupted_submission_quarantine(current, isolated_dispatch):
    item = admitted(current)
    isolated_dispatch.reset_mock()
    before = timezone.now() - timedelta(minutes=20)
    BulkDomainItem.objects.filter(pk=item.pk).update(status="submitting", submitted_at=before, lease_until=timezone.now() + timedelta(minutes=5))
    assert bulk_jobs.dispatch_bulk_items() == 0
    item.refresh_from_db()
    assert item.status == "submitting" and not isolated_dispatch.called


def test_confirmation_publishes_only_after_commit_and_never_after_failed_audit(current, isolated_dispatch):
    from django.db import connection
    from apps.audit.models import AuditLog
    from tests.test_bulk_domain_admission import preview, confirm
    batch = preview(current)
    def delivered(item_id):
        assert not connection.in_atomic_block
        assert BulkDomainItem.objects.get(pk=item_id).batch.confirmed_at is not None
    isolated_dispatch.side_effect = delivered
    confirm(batch)
    isolated_dispatch.assert_called_once_with(batch.items.get().pk)
    other = Domain.objects.create(user=current[1].user, name="audit-dispatch.test", tld="test", auto_renew=True)
    import uuid
    pending = create_bulk_preview(customer=current[1].user, key=uuid.uuid4(), action="auto_renew", domain_ids=[other.pk], target={"enabled": False})
    isolated_dispatch.reset_mock()
    real_create = AuditLog.objects.create
    def audit(**kwargs):
        if kwargs["action"] == "domain.bulk.confirmed":
            raise RuntimeError("audit unavailable")
        return real_create(**kwargs)
    with patch("apps.domains.bulk_admission.AuditLog.objects.create", side_effect=audit):
        with pytest.raises(RuntimeError):
            confirm(pending)
    assert not isolated_dispatch.called and pending.items.get().status == "ready"


def test_unconfirmed_history_is_neither_dispatched_nor_quarantined(current, isolated_dispatch):
    from tests.test_bulk_domain_admission import preview
    batch = preview(current)
    item = batch.items.get()
    BulkDomainItem.objects.filter(pk=item.pk).update(status="submitting", submitted_at=timezone.now() - timedelta(minutes=20))
    isolated_dispatch.reset_mock()
    assert bulk_jobs.dispatch_bulk_items() == 0
    bulk_jobs.run_bulk_item(item.pk)
    item.refresh_from_db()
    assert item.status == "submitting" and item.dispatch_token is None and not isolated_dispatch.called
