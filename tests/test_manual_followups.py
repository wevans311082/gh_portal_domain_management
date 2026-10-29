from copy import deepcopy
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.urls import reverse
from django.utils import timezone

from apps.admin_tools.manual_followups import capture_manual_followup, prepare_manual_followup, complete_manual_followup, manual_followup_summary
from apps.admin_tools.models import ManualDomainFollowup
from apps.audit.models import AuditLog
from apps.billing.models import Invoice
from apps.domains.models import Domain, DomainOrder
from apps.products.models import Package
from apps.provisioning.models import ProvisioningJob
from apps.provisioning.tasks import provision_hosting_account
from apps.provisioning.providers import ProvisioningProviderError
from apps.services.models import Service
from tests.agreement_helpers import attach_agreement
from tests.test_registration_submission import order, provider, submit
from tests.test_contact_change_recovery import owner

pytestmark = pytest.mark.django_db


@pytest.fixture
def package(settings):
    settings.WHM_HOST = "manual-whm.example.test"
    return Package.objects.create(name="Saved starter", slug="saved-starter", whm_package_name="starter",
        price_monthly=10, price_annually=100)


@pytest.fixture
def saved(order, owner, package):
    order.total_price = order.quoted_price = "12.00"
    order.save(update_fields=["total_price", "quoted_price"])
    order.refresh_from_db()
    return capture_manual_followup(order=order, actor=owner, hosting=True, package=package, invoice=True,
        notes="Private saved billing notes <script>plain text</script>")


@pytest.fixture
def registered(saved, order, provider):
    attach_agreement(order)
    assert submit(order, provider) == "501"
    order.refresh_from_db()
    order.domain = Domain.objects.create(user=order.user, name=order.domain_name, tld=order.tld,
        status="active", registrar_id="501")
    order.status, order.completed_at = "completed", timezone.now()
    order.save(update_fields=["domain", "status", "completed_at"])
    return saved


def test_capture_retains_exact_choices_without_financial_or_provider_side_effects(saved, order):
    assert saved.plan["package"]["annual_price"] == "100.00" and saved.plan["order"]["total_price"] == "12.00"
    assert not Service.objects.exists() and not Invoice.objects.exists() and not ProvisioningJob.objects.exists()
    assert not order.registration_contact.verification_evidence.exists()
    assert "Private saved billing notes" not in str(list(AuditLog.objects.filter(action__startswith="domain.manual_followup.").values("data")))


def test_immutable_choices_and_checksum_hold_unexpected_edits(saved, owner):
    original = deepcopy(saved.plan)
    saved.plan["notes"] = "replacement"
    with pytest.raises(ValidationError, match="cannot be replaced"):
        saved.save()
    saved.refresh_from_db()
    assert saved.plan == original
    ManualDomainFollowup.objects.filter(pk=saved.pk).update(plan=dict(original, notes="unexpected edit"))
    with pytest.raises(ValidationError, match="owner review"):
        prepare_manual_followup(order_id=saved.order_id, actor=owner)


def test_unregistered_request_keeps_followups_waiting(saved, owner):
    with pytest.raises(ValidationError, match="Register this unchanged"):
        prepare_manual_followup(order_id=saved.order_id, actor=owner)
    assert not Invoice.objects.exists() and not Service.objects.exists()


def test_local_materialization_is_atomic_and_replay_reuses_every_record(registered, owner):
    first = prepare_manual_followup(order_id=registered.order_id, actor=owner)
    second = prepare_manual_followup(order_id=registered.order_id, actor=owner)
    assert (first.service_id, first.invoice_id, first.job_id) == (second.service_id, second.invoice_id, second.job_id)
    assert Service.objects.count() == Invoice.objects.count() == ProvisioningJob.objects.count() == 1
    assert first.invoice.subtotal == 112 and first.invoice.notes.startswith("Private saved billing notes")
    assert first.service.invoice_id == first.invoice_id and first.order.invoice_id == first.invoice_id


def test_capture_price_survives_later_package_price_and_label_changes(registered, owner, package):
    package.price_annually, package.name = 999, "Renamed later"
    package.save(update_fields=["price_annually", "name"])
    record = prepare_manual_followup(order_id=registered.order_id, actor=owner)
    assert record.invoice.subtotal == 112
    assert record.invoice.line_items.filter(description__contains="Saved starter").exists()


@pytest.mark.parametrize("field,value", [("whm_package_name", "different"), ("provisioning_config", {"changed": True}), ("is_active", False)])
def test_routing_or_package_availability_change_requires_review(registered, owner, package, field, value):
    setattr(package, field, value)
    package.save(update_fields=[field])
    with pytest.raises(ValidationError, match="changed"):
        prepare_manual_followup(order_id=registered.order_id, actor=owner)
    assert not Invoice.objects.exists() and not Service.objects.exists()


def test_audit_failure_rolls_back_service_invoice_job_and_followup_marker(registered, owner):
    with patch.object(AuditLog.objects, "create", side_effect=DatabaseError), pytest.raises(DatabaseError):
        prepare_manual_followup(order_id=registered.order_id, actor=owner)
    registered.refresh_from_db()
    assert registered.prepared_at is None
    assert not Invoice.objects.exists() and not Service.objects.exists() and not ProvisioningJob.objects.exists()


def test_queue_delivery_replays_do_not_allocate_or_send_another_job(registered, owner):
    with patch.object(provision_hosting_account, "apply_async") as send:
        first = complete_manual_followup(order_id=registered.order_id, actor=owner)
        second = complete_manual_followup(order_id=registered.order_id, actor=owner)
    assert first.job_id == second.job_id and send.call_count == 1
    assert ProvisioningJob.objects.count() == 1


def test_failed_queue_delivery_reuses_the_same_worker_fenced_job(registered, owner):
    with patch.object(provision_hosting_account, "apply_async", side_effect=RuntimeError("PRIVATE-BROKER-ERROR")), pytest.raises(ValidationError):
        complete_manual_followup(order_id=registered.order_id, actor=owner)
    registered.refresh_from_db()
    assert registered.dispatch_state == "unknown" and registered.worker_started_at is None
    job_id = registered.job_id
    with patch.object(provision_hosting_account, "apply_async") as send:
        result = complete_manual_followup(order_id=registered.order_id, actor=owner)
    assert result.job_id == job_id and send.call_count == 1
    assert "PRIVATE" not in str(list(ManualDomainFollowup.objects.values()))
    assert ProvisioningJob.objects.count() == Invoice.objects.count() == Service.objects.count() == 1


def test_late_failed_delivery_cannot_overwrite_newer_acknowledgement(registered, owner):
    calls = 0
    def send(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            complete_manual_followup(order_id=registered.order_id, actor=owner)
            raise RuntimeError("old delivery failure")
    with patch.object(provision_hosting_account, "apply_async", side_effect=send), pytest.raises(ValidationError):
        complete_manual_followup(order_id=registered.order_id, actor=owner)
    registered.refresh_from_db()
    assert registered.dispatch_state == "dispatched" and calls == 2


def test_worker_duplicate_delivery_calls_hosting_provider_once(registered, owner):
    with patch.object(provision_hosting_account, "apply_async"):
        record = complete_manual_followup(order_id=registered.order_id, actor=owner)
    provider = Mock(provider_key="whm", create_site=Mock(return_value={"ok": True, "password": "PRIVATE-SUCCESS-CREDENTIAL"}))
    with patch("apps.provisioning.tasks.get_provider_for_service", return_value=provider), patch("apps.notifications.services.send_notification"):
        provision_hosting_account.run(record.service_id, record.job_id)
        provision_hosting_account.run(record.service_id, record.job_id)
    assert provider.create_site.call_count == 1
    record.job.refresh_from_db()
    assert record.job.status == "completed"
    assert record.job.result_data["provider_acknowledged"] is True
    assert "PRIVATE-SUCCESS-CREDENTIAL" not in str(record.job.result_data)


def test_uncertain_hosting_failure_never_automatically_creates_again(registered, owner):
    with patch.object(provision_hosting_account, "apply_async"):
        record = complete_manual_followup(order_id=registered.order_id, actor=owner)
    provider = Mock(provider_key="whm", create_site=Mock(side_effect=ProvisioningProviderError("PRIVATE-PROVIDER-ERROR")))
    with patch("apps.provisioning.tasks.get_provider_for_service", return_value=provider), patch.object(provision_hosting_account, "retry") as retry:
        provision_hosting_account.run(record.service_id, record.job_id)
        provision_hosting_account.run(record.service_id, record.job_id)
    assert provider.create_site.call_count == 1 and not retry.called
    record.job.refresh_from_db()
    assert record.job.status == "failed" and "PRIVATE" not in record.job.last_error
    replacement = ProvisioningJob.objects.create(service_id=record.service_id, idempotency_key="unapproved-replacement")
    with patch("apps.provisioning.tasks.get_provider_for_service") as bypass:
        provision_hosting_account.run(record.service_id, replacement.pk)
    bypass.assert_not_called()
    replacement.refresh_from_db()
    assert replacement.status == "failed"


def test_domain_only_operator_cannot_resume_billing_or_hosting_choices(registered, django_user_model):
    actor = django_user_model.objects.create_user(email="domain-followup-only@example.test", password="testpass", is_staff=True)
    actor.groups.add(Group.objects.get(name="Portal Domain Operations"))
    with pytest.raises(PermissionDenied):
        complete_manual_followup(order_id=registered.order_id, actor=actor)
    summary = manual_followup_summary(order=registered.order, actor=actor)
    assert "notes" not in summary and "hosting_price" not in summary and "package" not in summary
    assert not summary["can_resume"] and not Invoice.objects.exists()


def test_actor_revocation_before_worker_claim_blocks_hosting(registered, owner):
    with patch.object(provision_hosting_account, "apply_async"):
        record = complete_manual_followup(order_id=registered.order_id, actor=owner)
    type(owner).objects.filter(pk=owner.pk).update(is_active=False)
    with patch("apps.provisioning.tasks.get_provider_for_service") as provider:
        provision_hosting_account.run(record.service_id, record.job_id)
    provider.assert_not_called()
    record.refresh_from_db()
    assert record.worker_started_at is None
    type(owner).objects.filter(pk=owner.pk).update(is_active=True)
    with patch.object(provision_hosting_account, "apply_async") as redeliver:
        complete_manual_followup(order_id=registered.order_id, actor=owner)
    redeliver.assert_called_once()
    provider = Mock(provider_key="whm", create_site=Mock(return_value={"ok": True}))
    with patch("apps.provisioning.tasks.get_provider_for_service", return_value=provider), patch("apps.notifications.services.send_notification"):
        provision_hosting_account.run(record.service_id, record.job_id)
    assert provider.create_site.call_count == 1


def test_duplicate_delivery_while_provider_is_running_cannot_create_a_second_account(registered, owner):
    with patch.object(provision_hosting_account, "apply_async"):
        record = complete_manual_followup(order_id=registered.order_id, actor=owner)
    def create(**kwargs):
        provision_hosting_account.run(record.service_id, record.job_id)
        return {"ok": True}
    provider = Mock(provider_key="whm", create_site=Mock(side_effect=create))
    with patch("apps.provisioning.tasks.get_provider_for_service", return_value=provider), patch("apps.notifications.services.send_notification"):
        provision_hosting_account.run(record.service_id, record.job_id)
    assert provider.create_site.call_count == 1


def test_package_change_after_delivery_stops_the_worker_before_provider_access(registered, owner, package):
    with patch.object(provision_hosting_account, "apply_async"):
        record = complete_manual_followup(order_id=registered.order_id, actor=owner)
    package.whm_package_name = "changed-after-delivery"
    package.save(update_fields=["whm_package_name"])
    with patch("apps.provisioning.tasks.get_provider_for_service") as provider:
        provision_hosting_account.run(record.service_id, record.job_id)
    provider.assert_not_called()
    record.refresh_from_db()
    assert record.worker_started_at is None


def test_whm_account_change_after_delivery_stops_before_hosting_admission(registered, owner, settings):
    with patch.object(provision_hosting_account, "apply_async"):
        record = complete_manual_followup(order_id=registered.order_id, actor=owner)
    settings.WHM_HOST = "another-account.invalid"
    with patch("apps.provisioning.tasks.get_provider_for_service") as provider:
        provision_hosting_account.run(record.service_id, record.job_id)
    provider.assert_not_called()
    record.refresh_from_db()
    assert record.worker_started_at is None


def test_hosting_capture_requires_a_known_whm_account(order, owner, package, settings):
    settings.WHM_HOST = ""
    with pytest.raises(ValidationError, match="Configure the WHM host"):
        capture_manual_followup(order=order, actor=owner, hosting=True, package=package, invoice=False)
    assert not ManualDomainFollowup.objects.exists() and not ProvisioningJob.objects.exists()


def test_provider_uses_the_client_frozen_at_manual_admission(registered, owner, settings):
    from apps.core.runtime_settings import get_runtime_setting
    original_host = get_runtime_setting("WHM_HOST", "")
    with patch.object(provision_hosting_account, "apply_async"):
        record = complete_manual_followup(order_id=registered.order_id, actor=owner)
    provider = Mock(provider_key="whm", create_site=Mock(return_value={"ok": True}))
    def build(service):
        settings.WHM_HOST = "changed-after-admission.invalid"
        return provider
    with patch("apps.provisioning.tasks.get_provider_for_service", side_effect=build), patch("apps.notifications.services.send_notification"):
        provision_hosting_account.run(record.service_id, record.job_id)
    assert provider.create_site.call_count == 1 and provider.client.host == original_host


def test_http_process_checks_saved_extra_capabilities_before_mutating_or_registering(saved, owner, client, django_user_model):
    from apps.accounts.mfa import credential_fingerprint
    actor = django_user_model.objects.create_user(email="manual-domain-role@example.test", password="testpass", is_staff=True, mfa_enabled=True)
    actor.groups.add(Group.objects.get(name="Portal Domain Operations"))
    client.force_login(actor)
    session = client.session
    session["_mfa_verified_at"] = timezone.now().timestamp()
    session["_mfa_verified_user_id"] = actor.pk
    session["_mfa_verified_hash"] = credential_fingerprint(actor)
    session.save()
    with patch("apps.admin_tools.domain_order_views.register_domain_order") as register:
        response = client.post(reverse("admin_tools:domain_order_action", kwargs={"pk": saved.order_id, "action": "process"}))
    assert response.status_code == 403 and not register.called
    saved.order.refresh_from_db()
    assert saved.order.status == "paid" and not Invoice.objects.exists() and not Service.objects.exists()


def test_complete_manual_sale_survives_customer_review_and_resumes_once(client, owner, order, package, provider, settings):
    from tests.security_helpers import login_owner
    from tests.agreement_helpers import publish_test_agreement
    from django.test import Client
    from apps.domains.models import TLDPricing
    TLDPricing.objects.create(tld="com", registration_cost=8, is_active=True)
    login_owner(client, owner)
    settings.RESELLERCLUB_CUSTOMER_ID = "999"
    with patch("apps.admin_tools.manual_order_views._load_whm_hosting_packages", return_value=([], "fixture")):
        response = client.post(reverse("admin_tools:manual_domain_register"), {"domain_name": "saved-manual-flow.com",
            "user_id": order.user_id, "contact_id": order.registration_contact_id, "years": "1",
            "create_hosting": "on", "whm_package_name": package.whm_package_name, "create_invoice": "on",
            "invoice_amount": "150.00", "notes": "Saved override and notes"})
    assert response.status_code == 302
    request = DomainOrder.objects.get(domain_name="saved-manual-flow.com")
    assert not Invoice.objects.exists() and not Service.objects.exists() and not ProvisioningJob.objects.exists()
    publish_test_agreement()
    customer = Client()
    customer.force_login(order.user)
    review_url = reverse("domains:request_agreements", kwargs={"kind": "registration", "pk": request.pk})
    token = customer.get(review_url).context["form"].initial["review"]
    assert customer.post(review_url, {"review": token, "accepted": "on"}).status_code == 302
    action_url = reverse("admin_tools:domain_order_action", kwargs={"pk": request.pk, "action": "process"})
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider), \
            patch("apps.domains.tasks._build_nameservers", return_value=["ns1.example.com", "ns2.example.com"]), \
            patch.object(provision_hosting_account, "apply_async") as send:
        assert client.post(action_url).status_code == 302
        assert client.post(action_url).status_code == 302
    request.refresh_from_db()
    assert request.status == "completed" and provider.register_domain.call_count == send.call_count == 1
    assert Invoice.objects.count() == Service.objects.count() == ProvisioningJob.objects.count() == 1
    assert request.invoice.subtotal == 150 and request.invoice.notes == "Saved override and notes"
    assert request.manual_followup.invoice_id == request.invoice_id


def test_cancelled_request_history_does_not_allow_a_duplicate_manual_order(client, owner, order):
    from tests.security_helpers import login_owner
    order.status = "cancelled"
    order.save(update_fields=["status"])
    login_owner(client, owner)
    with patch("apps.admin_tools.manual_order_views._load_whm_hosting_packages", return_value=([], "fixture")):
        response = client.post(reverse("admin_tools:manual_domain_register"), {"domain_name": order.domain_name,
            "user_id": order.user_id, "contact_id": order.registration_contact_id, "years": "1"})
    assert response.status_code == 200 and b"platform order already exists" in response.content
    assert DomainOrder.objects.count() == 1 and not ManualDomainFollowup.objects.exists()


def test_saved_choices_protect_unsubmitted_order_from_deletion_but_allow_cancellation(client, owner, saved):
    from tests.security_helpers import login_owner
    login_owner(client, owner)
    url = lambda action: reverse("admin_tools:domain_order_action", kwargs={"pk": saved.order_id, "action": action})
    assert client.post(url("delete")).status_code == 302
    assert DomainOrder.objects.filter(pk=saved.order_id).exists()
    assert client.post(url("cancel"), {"reason": "Customer withdrew before registration"}).status_code == 302
    saved.order.refresh_from_db()
    assert saved.order.status == "cancelled" and not Invoice.objects.exists() and not Service.objects.exists()


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "1.001", "100000000", "invalid"])
def test_invalid_invoice_override_does_not_capture_choices(order, owner, value):
    with pytest.raises(ValidationError):
        capture_manual_followup(order=order, actor=owner, hosting=False, package=None, invoice=True, invoice_amount=value)
    assert not ManualDomainFollowup.objects.exists()


@pytest.mark.parametrize("notes", ["invalid\x00notes", "invalid\x7fnotes"])
def test_control_characters_in_notes_are_rejected_before_capture(order, owner, notes):
    with pytest.raises(ValidationError, match="control characters"):
        capture_manual_followup(order=order, actor=owner, hosting=False, package=None, invoice=True, notes=notes)
    assert not ManualDomainFollowup.objects.exists()
