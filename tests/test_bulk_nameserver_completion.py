from datetime import timedelta
from unittest.mock import patch
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.bulk_completion import confirm_nameservers_registrar_completed, NAMESERVER_EVENT
from apps.domains.bulk_controls import set_bulk_paused
from apps.domains.bulk_remote import execute_remote_nameservers
from apps.domains.bulk_reservations import release_item
from apps.domains.models import BulkDomainItem, DomainOperationReservation
from apps.domains.contact_change_confirmation import ContactChangeAction
from apps.products.models import Package
from apps.services.models import Service
from tests.test_bulk_domain_nameservers import admitted_nameservers, NameserverProvider
from tests.test_bulk_domain_observation import Reader
from tests.test_bulk_nameserver_observation import delegation
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db(transaction=True)


class Reviewer(Reader):
    def get_contact_change_action(self, order_id, action_id):
        self.reads.append((order_id, action_id))
        return ContactChangeAction("501", "812", "ReviewedNameserverType", "ReviewedCode", str(int((timezone.now() - timedelta(minutes=10)).timestamp())))


def setup(current):
    item, values = admitted_nameservers(current)
    execute_remote_nameservers(item.pk, client=NameserverProvider(item))
    owner = User.objects.create_user(email="nameserver-review-owner@example.test", password="test", is_staff=True, is_superuser=True)
    set_bulk_paused(item.pk, owner_id=owner.pk, paused=True, reason="Pause exact nameserver operation for terminal provider review.")
    old = timezone.now() - timedelta(minutes=20)
    BulkDomainItem.objects.filter(pk=item.pk).update(submitted_at=old, updated_at=old)
    item.refresh_from_db()
    return item, values, owner, Reviewer(item, delegation(item, list(reversed(values))))


def confirm(item, owner, client):
    return confirm_nameservers_registrar_completed(item.pk, owner_id=owner.pk, action_id="812", action_type="ReviewedNameserverType", reference="CASE-NS-812", reason="Provider confirmed this exact completed nameserver action and live delegation.", provider_success_confirmed=True, workers_stopped_confirmed=True, requested_operation_confirmed=True, client=client)


def test_terminal_review_retains_all_hosts_mirrors_prefix_and_replays_without_reads(current):
    item, values, owner, client = setup(current)
    result = confirm(item, owner, client)
    current[0].refresh_from_db()
    assert result.status == "completed" and result.registrar_completed_at is not None
    assert result.hosting_status == "not_required" and result.completed_at is not None
    assert current[0].registrar_nameservers == list(reversed(values))
    assert current[0].nameserver1 == values[-1] and current[0].nameserver4 == values[-4]
    assert not DomainOperationReservation.objects.filter(item=item).exists()
    reads = list(client.reads)
    assert confirm(item, owner, client).status == "completed" and client.reads == reads
    assert AuditLog.objects.filter(action=NAMESERVER_EVENT).count() == 1


def test_active_hosting_keeps_followup_pending_and_never_reposts_registrar(current):
    item, values, owner, client = setup(current)
    package = Package.objects.create(name="Hosted followup", slug="hosted-followup", price_monthly=10, price_annually=100)
    Service.objects.create(user=current[1].user, package=package, domain_name=item.name, status="active", cpanel_username="hosteduser")
    result = confirm(item, owner, client)
    assert result.status == "awaiting" and result.completed_at is None
    assert result.registrar_completed_at is not None and result.hosting_status == "pending"
    assert DomainOperationReservation.objects.filter(item=item).exists()
    with pytest.raises(ValidationError, match="hosting follow-up"):
        release_item(result)
    reads = list(client.reads)
    assert confirm(item, owner, client).hosting_status == "pending" and client.reads == reads
    from apps.domains.bulk_jobs import run_bulk_item
    set_bulk_paused(item.pk, owner_id=owner.pk, paused=False, reason="Continue only the independent hosting follow-up review.")
    with patch("apps.domains.bulk_jobs.execution_enabled", return_value=True), patch("apps.domains.bulk_reconciliation.observe_remote_nameservers", side_effect=AssertionError("Registrar stage must not restart")):
        run_bulk_item(item.pk)


@pytest.mark.parametrize("hosts", [2, 12])
def test_partial_live_delegation_never_becomes_completion(current, hosts):
    item, values, owner, client = setup(current)
    client.payload = delegation(item, values[:hosts])
    with pytest.raises(ValidationError, match="live registrar nameservers"):
        confirm(item, owner, client)
    current[0].refresh_from_db()
    assert current[0].registrar_nameservers == [] and DomainOperationReservation.objects.filter(item=item).exists()


def test_cached_delegation_refresh_cannot_replace_independent_terminal_evidence(current):
    item, values, owner, client = setup(current)
    current[0].set_nameserver_cache(values[:2])
    current[0].save()
    assert confirm(item, owner, client).status == "completed"
    current[0].refresh_from_db()
    assert len(current[0].registrar_nameservers) == 13


def test_completion_audit_failure_rolls_back_cache_stage_and_slot_release(current):
    item, values, owner, client = setup(current)
    real_create = AuditLog.objects.create
    def audit(**kwargs):
        if kwargs["action"] == NAMESERVER_EVENT:
            raise RuntimeError("audit unavailable")
        return real_create(**kwargs)
    with patch("apps.domains.bulk_completion.AuditLog.objects.create", side_effect=audit):
        with pytest.raises(RuntimeError):
            confirm(item, owner, client)
    item.refresh_from_db()
    current[0].refresh_from_db()
    assert item.registrar_completed_at is None and current[0].registrar_nameservers == []
    assert DomainOperationReservation.objects.filter(item=item).exists()
