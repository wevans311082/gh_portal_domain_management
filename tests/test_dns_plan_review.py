from unittest.mock import patch
import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.domains.dns_inventory import collect_dns_inventory
from apps.domains.dns_plan_review import create_record_plan, approve_record_plan
from apps.domains.models import DomainDNSRecordPlan
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_dns_inventory_stage import admitted

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def ready(admitted):
    change, source, target, registrar, domain = admitted
    collect_dns_inventory(change.pk, source_client=source, target_client=target, registrar_client=registrar)
    return change, source, target, registrar, domain


def create(ready, **kwargs):
    change, source, target, registrar, domain = ready
    return create_record_plan(actor=change.requested_by, change_id=change.pk, source_client=source,
        target_client=target, registrar_client=registrar, **kwargs)


def approve(ready, plan, **kwargs):
    values = {"actor": ready[0].requested_by, "plan_id": plan.pk, "digest": plan.digest, "confirmed": True,
        "acknowledgements": plan.plan["required_acknowledgements"], "source_client": ready[1],
        "target_client": ready[2], "registrar_client": ready[3]}
    values.update(kwargs)
    return approve_record_plan(**values)


def test_record_plan_creation_and_approval_are_durable_idempotent_and_offline(ready):
    with patch("requests.request", side_effect=AssertionError("No provider calls")):
        plan = create(ready)
        assert create(ready).pk == plan.pk
        assert approve(ready, plan).approved_by_id == ready[0].requested_by_id
        assert approve(ready, plan).pk == plan.pk
    ready[0].refresh_from_db()
    assert ready[0].status == "plan_approved" and ready[0].approved_record_plan_id == plan.pk
    assert ready[0].zone_submitted_at is None
    assert ready[1].list_dns_records.call_count == 1


def test_ttl_revision_retains_prior_plan_and_approval_is_explicit(ready):
    first = create(ready)
    ref = first.plan["entries"][0]["source_ref"]
    second = create(ready, ttl_overrides={ref: 7200})
    assert first.pk != second.pk and first.plan["entries"][0]["record"]["ttl"] == 3600
    assert second.plan["entries"][0]["record"]["ttl"] == 7200
    approve(ready, second)
    with pytest.raises(ValidationError):
        approve(ready, first)


def test_foreign_actor_cannot_create_or_approve_plan(ready, django_user_model):
    user = django_user_model.objects.create_user(email="foreign-plan@example.test", password="testpass")
    plan = create(ready)
    with pytest.raises(PermissionDenied):
        create_record_plan(actor=user, change_id=ready[0].pk)
    with pytest.raises(PermissionDenied):
        approve(ready, plan, actor=user)


def test_changed_source_configuration_or_tampered_plan_prevents_approval(ready):
    plan = create(ready)
    ready[2].api_token = "ROTATED"
    with pytest.raises(ValidationError):
        approve(ready, plan)
    ready[2].api_token = "PRIVATE-TOKEN"
    DomainDNSRecordPlan.objects.filter(pk=plan.pk).update(plan={"tampered": True})
    with pytest.raises(ValidationError):
        approve(ready, plan)


def test_approval_audit_failure_rolls_back_approval_and_parent_transition(ready):
    plan = create(ready)
    with patch("apps.domains.dns_plan_review.AuditLog.objects.create", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            approve(ready, plan)
    plan.refresh_from_db()
    ready[0].refresh_from_db()
    assert plan.approved_at is None and ready[0].approved_record_plan_id is None and ready[0].status == "plan_review"


def test_plan_history_cannot_be_rewritten_or_approval_pointer_reset(ready):
    plan = create(ready)
    approve(ready, plan)
    plan.plan = {"changed": True}
    with pytest.raises(ValidationError):
        plan.save()
    ready[0].refresh_from_db()
    ready[0].approved_record_plan = None
    with pytest.raises(ValidationError):
        ready[0].save()


def test_explicit_confirmation_and_acknowledgement_types_are_required(ready):
    plan = create(ready)
    with pytest.raises(ValidationError):
        approve(ready, plan, confirmed="true")
    with pytest.raises(ValidationError):
        approve(ready, plan, acknowledgements=["unknown_impact"])
