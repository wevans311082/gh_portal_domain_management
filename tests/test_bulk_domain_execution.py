from unittest.mock import patch
import pytest
from django.core.exceptions import ValidationError
from apps.accounts.models import User
from apps.domains.bulk_execution import execute_local_preference
from apps.domains.models import DomainOperationReservation
from apps.audit.models import AuditLog
from tests.test_bulk_domain_admission import preview, confirm
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db


def admitted(current):
    current[0].auto_renew = True
    current[0].save(update_fields=["auto_renew"])
    batch = preview(current, action="auto_renew", target={"enabled": False})
    confirm(batch)
    return batch.items.get()


def test_local_preference_executes_once_without_provider_http(current):
    item = admitted(current)
    with patch("apps.domains.resellerclub_client.ResellerClubClient.__init__", side_effect=AssertionError("No provider HTTP")):
        assert execute_local_preference(item.pk).status == "completed"
        assert execute_local_preference(item.pk).status == "completed"
    current[0].refresh_from_db()
    assert current[0].auto_renew is False
    assert not DomainOperationReservation.objects.exists()
    assert AuditLog.objects.filter(action="domain.bulk.preference_completed").count() == 1


@pytest.mark.parametrize("change", ["owner_inactive", "owner_promoted", "preference_changed", "reservation_missing"])
def test_changed_authority_or_subject_fails_before_local_mutation(current, change):
    item = admitted(current)
    if change == "owner_inactive":
        User.objects.filter(pk=current[1].user_id).update(is_active=False)
    elif change == "owner_promoted":
        User.objects.filter(pk=current[1].user_id).update(is_staff=True)
    elif change == "preference_changed":
        current[0].auto_renew = False
        current[0].save(update_fields=["auto_renew"])
    else:
        DomainOperationReservation.objects.all().delete()
    assert execute_local_preference(item.pk).status == "failed"
    assert not DomainOperationReservation.objects.exists()
    assert not AuditLog.objects.filter(action="domain.bulk.preference_completed").exists()


def test_audit_failure_rolls_back_preference_result_and_reservation(current):
    item = admitted(current)
    with patch("apps.domains.bulk_execution.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            execute_local_preference(item.pk)
    current[0].refresh_from_db()
    item.refresh_from_db()
    assert current[0].auto_renew is True and item.status == "queued"
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_remote_operations_cannot_use_local_executor(current):
    batch = preview(current)
    confirm(batch)
    item = batch.items.get()
    with pytest.raises(ValidationError, match="only local"):
        execute_local_preference(item.pk)
    assert DomainOperationReservation.objects.filter(item=item).exists()
