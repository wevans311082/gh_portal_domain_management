from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.contact_change_confirmation import ContactChangeAction
from apps.domains.contact_change_recovery import recover_contact_change
from apps.domains.models import DomainContactChange
from tests.test_contact_change_admission import current, request
from tests.test_contact_change_submission import api
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_superuser(email="recovery-owner@example.test", password="testpass")


def uncertain(current):
    job = request(current)
    stale = timezone.now() - timedelta(minutes=20)
    DomainContactChange.objects.filter(pk=job.pk).update(status="unknown", submitted_at=stale, updated_at=stale,
        role_ids={"reg": "301", "admin": "301", "tech": "301", "billing": "301"})
    job.refresh_from_db()
    return job


def recover(job, owner, **kwargs):
    options = dict(owner_id=owner.pk, action="link-action", action_id="701", reference="CASE-701",
        reason="Provider support confirmed the exact action for this request.")
    options.update(kwargs)
    return recover_contact_change(job.pk, **options)


def test_linking_known_action_only_resumes_read_only_observation(current, owner, api):
    job = uncertain(current)
    old_token = job.token
    api.get_contact_change_action.return_value = ContactChangeAction("501", "701", "ModifyContact", "AwaitingIRTP", "")
    assert recover(job, owner, client=api) == "awaiting"
    job.refresh_from_db()
    assert job.action_id == "701" and job.token != old_token
    api.change_domain_contacts.assert_not_called()
    api.create_contact.assert_not_called()
    current[1].refresh_from_db()
    assert current[1].registrar_contact_ids["reg"] == "201"
    assert AuditLog.objects.filter(action="domain.contact_change.link_action", user=owner).exists()


@pytest.mark.parametrize("field,value", [("order_id", "502"), ("action_id", "702")])
def test_foreign_action_evidence_cannot_be_linked(current, owner, api, field, value):
    job = uncertain(current)
    data = dict(order_id="501", action_id="701", action_type="ModifyContact", status="Completed", completed_at="")
    data[field] = value
    api.get_contact_change_action.return_value = ContactChangeAction(**data)
    with pytest.raises(ValidationError):
        recover(job, owner, client=api)
    assert DomainContactChange.objects.get(pk=job.pk).status == "unknown"


def test_recent_or_live_worker_cannot_be_recovered(current, owner, api):
    job = uncertain(current)
    DomainContactChange.objects.filter(pk=job.pk).update(lease_until=timezone.now() + timedelta(minutes=1))
    with pytest.raises(ValidationError, match="15 minutes"):
        recover(job, owner, client=api)
    api.get_contact_change_action.assert_not_called()


def test_only_owner_may_link_action(current, api):
    job = uncertain(current)
    with pytest.raises(ValidationError, match="active owner"):
        recover(job, current[1].user, client=api)
    api.get_contact_change_action.assert_not_called()


def test_post_marker_submission_cannot_be_cancelled_as_preparation(current, owner):
    job = uncertain(current)
    with pytest.raises(ValidationError, match="without a submission marker"):
        recover(job, owner, action="cancel-preparation", action_id="")


def test_stale_preparation_is_cancelled_and_fenced_without_provider_work(current, owner):
    job = uncertain(current)
    DomainContactChange.objects.filter(pk=job.pk).update(status="preparing", submitted_at=None, role_ids={})
    assert recover(job, owner, action="cancel-preparation", action_id="") == "cancelled"
    assert DomainContactChange.objects.get(pk=job.pk).token != job.token


def test_state_changed_during_provider_read_prevents_recovery(current, owner, api):
    job = uncertain(current)
    def action(*args):
        DomainContactChange.objects.filter(pk=job.pk).update(updated_at=timezone.now())
        return ContactChangeAction("501", "701", "ModifyContact", "Completed", "")
    api.get_contact_change_action.side_effect = action
    with pytest.raises(ValidationError, match="changed during"):
        recover(job, owner, client=api)


def test_command_does_not_expose_provider_exception(current, owner):
    job = uncertain(current)
    with patch("apps.domains.resellerclub_client.ResellerClubClient", side_effect=RuntimeError("secret-provider-token")):
        with pytest.raises(CommandError) as result:
            call_command("reconcile_contact_change", change_id=job.pk, owner_id=owner.pk, action="link-action",
                action_id="701", provider_reference="CASE-701", reason="Reviewed provider action history.")
    assert "secret-provider-token" not in str(result.value)
