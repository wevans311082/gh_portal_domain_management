"""Retain manual-sale choices and materialize them once after registration."""
from decimal import Decimal, InvalidOperation
import hashlib
import json
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.billing.models import Invoice
from apps.billing.services import create_invoice, LineItemSpec
from apps.domains.models import Domain, DomainOrder, DomainRegistrationAttempt
from apps.products.models import Package
from apps.provisioning.models import ProvisioningJob
from apps.services.models import Service
from .models import ManualDomainFollowup


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= Decimal("99999999.99") or amount != amount.quantize(Decimal("0.01")):
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError("Use a non-negative invoice amount with at most two decimal places within the invoice limit.") from None
    return str(amount.quantize(Decimal("0.01")))


def _actor(actor_id, plan):
    actor = User.objects.get(pk=actor_id)
    capabilities = ["manage_domains"]
    if plan["hosting"]:
        capabilities.append("manage_services")
    if plan["invoice"]:
        capabilities.append("manage_billing")
    if (not actor.is_active or not actor.is_staff
            or not all(actor.has_perm("admin_tools." + capability) for capability in capabilities)):
        raise PermissionDenied("The saved choices require current domain, hosting and billing permissions as applicable.")
    return actor


def _order_values(order):
    return {field: str(getattr(order, field)) for field in ("user_id", "domain_name", "tld", "registration_years",
        "quoted_price", "total_price", "privacy_enabled", "auto_renew", "dns_provider")}


def _package_values(package):
    return {"id": package.pk, "name": package.name, "whm_name": package.whm_package_name,
        "provider": package.provisioning_provider, "config_digest": _digest(package.provisioning_config),
        "annual_price": _money(package.price_annually), "whm_scope": _whm_scope()}


def _whm_scope(client=None):
    if client is None:
        from apps.core.runtime_settings import get_runtime_setting, get_runtime_int
        values = {"host": get_runtime_setting("WHM_HOST", ""), "port": get_runtime_int("WHM_PORT", 2087),
            "username": get_runtime_setting("WHM_USERNAME", "root")}
    else:
        values = {"host": client.host, "port": client.port, "username": client.username}
    return _digest(values)


def _intact(record):
    if (not isinstance(record.plan, dict) or record.plan_digest != _digest(record.plan)
            or record.plan.get("version") != 1 or type(record.plan.get("hosting")) is not bool
            or type(record.plan.get("invoice")) is not bool):
        raise ValidationError("The retained manual choices need owner review.")
    if record.plan["hosting"] and not isinstance(record.plan.get("package"), dict):
        raise ValidationError("The retained package choices need owner review.")
    if record.package_id != (record.plan["package"].get("id") if record.plan["hosting"] else None):
        raise ValidationError("The retained package association needs owner review.")
    return record.plan


def _audit(record, actor, action):
    AuditLog.objects.create(user=actor, action="domain.manual_followup." + action, model_name="DomainOrder",
        object_id=str(record.order_id), data={"followup_id": record.pk})


def authorize_manual_followup(*, order_id, actor):
    record = ManualDomainFollowup.objects.filter(order_id=order_id).first()
    if record:
        _actor(actor.pk, _intact(record))


def manual_followup_summary(*, order, actor):
    record = ManualDomainFollowup.objects.select_related("job").filter(order=order).first()
    if record is None:
        return None
    try:
        plan = _intact(record)
    except ValidationError:
        return {"intact": False}
    result = {"intact": True, "hosting": plan["hosting"], "invoice_requested": plan["invoice"],
        "prepared": bool(record.prepared_at), "can_resume": False}
    if actor.has_perm("admin_tools.view_services") or actor.has_perm("admin_tools.manage_services"):
        result.update(package=plan["package"]["name"] if plan["hosting"] else "", service_id=record.service_id,
            job_id=record.job_id, job_status=record.job.get_status_display() if record.job_id else "Not created",
            dispatch_status=record.get_dispatch_state_display(),
            worker_requires_review=bool(record.worker_started_at and record.job_id and record.job.status in {"failed", "retry"}))
    if actor.has_perm("admin_tools.view_billing") or actor.has_perm("admin_tools.manage_billing"):
        result.update(override=plan["override"], notes=plan["notes"], invoice_id=record.invoice_id,
            hosting_price=plan["package"]["annual_price"] if plan["hosting"] else None)
    try:
        _actor(actor.pk, plan)
        pending = (not record.prepared_at or (record.job_id and not record.worker_started_at
            and (record.dispatch_state != "dispatched" or record.job.status == "failed")))
        result["can_resume"] = order.status == "completed" and bool(pending)
    except PermissionDenied:
        pass
    return result


@transaction.atomic
def capture_manual_followup(*, order, actor, hosting, package, invoice, invoice_amount="", notes=""):
    if (order.invoice_id or order.domain_id or order.registrar_order_id
            or DomainRegistrationAttempt.objects.filter(order=order).exists()):
        raise ValidationError("Save manual choices with a new request before registrar submission.")
    if type(hosting) is not bool or type(invoice) is not bool or not isinstance(notes, str) or len(notes) > 4000:
        raise ValidationError("Use valid manual choices and at most 4,000 characters of notes.")
    if any((ord(char) < 32 and char not in "\r\n\t") or ord(char) == 127 for char in notes):
        raise ValidationError("Use plain invoice notes without control characters.")
    plan = {"version": 1, "order": _order_values(order), "hosting": hosting, "invoice": invoice,
        "override": _money(invoice_amount) if invoice and invoice_amount else None, "notes": notes,
        "package": None}
    actor = _actor(actor.pk, plan)
    customer = User.objects.select_for_update().get(pk=order.user_id)
    if not customer.is_active or customer.is_staff or customer.is_superuser:
        raise ValidationError("Select an active customer account for this manual request.")
    if hosting:
        from apps.core.runtime_settings import get_runtime_setting
        if not get_runtime_setting("WHM_HOST", "") or not get_runtime_setting("WHM_USERNAME", "root"):
            raise ValidationError("Configure the WHM host and account before saving hosting choices.")
        if package is None:
            raise ValidationError("Select a hosting package for this request.")
        package = Package.objects.select_for_update().get(pk=package.pk)
        if not package.is_active or package.provisioning_provider != "whm" or not package.whm_package_name:
            raise ValidationError("Select an active WHM package for this manual hosting request.")
        plan["package"] = _package_values(package)
    record = ManualDomainFollowup.objects.create(order=order, created_by=actor, package=package if hosting else None,
        plan=plan, plan_digest=_digest(plan),
        dispatch_state="pending" if hosting else "not_required")
    _audit(record, actor, "captured")
    return record


def _registered(order, plan):
    if _order_values(order) != plan["order"] or order.status != "completed" or not order.domain_id:
        raise ValidationError("Register this unchanged manual request before completing its saved choices.")
    customer = User.objects.select_for_update().get(pk=order.user_id)
    domain = Domain.objects.select_for_update().get(pk=order.domain_id)
    if (domain.user_id != order.user_id or domain.name != order.domain_name
            or not order.registrar_order_id or domain.registrar_id != order.registrar_order_id):
        raise ValidationError("The registered domain association needs review before completing saved choices.")
    attempt = DomainRegistrationAttempt.objects.filter(order=order, status="accepted", legacy_review_required=False,
        remote_order_id=order.registrar_order_id).first()
    if attempt is None or attempt.provider_scope != order.registrar_contact_ids.get("_scope"):
        raise ValidationError("Retained registrar acknowledgement is required before completing saved choices.")
    if not customer.is_active or customer.is_staff or customer.is_superuser:
        raise ValidationError("The customer account needs review before completing saved choices.")


def _locked_order(order_id):
    snapshot = DomainOrder.objects.only("user_id", "domain_id").get(pk=order_id)
    User.objects.select_for_update().get(pk=snapshot.user_id)
    if snapshot.domain_id:
        Domain.objects.select_for_update().get(pk=snapshot.domain_id)
    order = DomainOrder.objects.select_for_update().get(pk=order_id)
    if order.user_id != snapshot.user_id or order.domain_id != snapshot.domain_id:
        raise ValidationError("The manual order association changed. Reload before completing saved choices.")
    return order


def _package(plan):
    package = Package.objects.select_for_update().get(pk=plan["package"]["id"])
    current = _package_values(package)
    # Prices and labels are retained from the original choice. Routing/config
    # changes require review; never silently provision against another contract.
    if (not package.is_active or any(current[key] != plan["package"][key]
            for key in ("id", "whm_name", "provider", "config_digest", "whm_scope"))):
        raise ValidationError("The saved hosting package or provider configuration changed. Owner review is required.")
    return package


@transaction.atomic
def prepare_manual_followup(*, order_id, actor):
    if not ManualDomainFollowup.objects.filter(order_id=order_id).exists():
        return None
    order = _locked_order(order_id)
    record = ManualDomainFollowup.objects.select_for_update().filter(order=order).first()
    if record is None:
        return None
    plan = _intact(record)
    actor = _actor(actor.pk, plan)
    _registered(order, plan)
    if record.prepared_at:
        if record.service_id and not Service.objects.filter(pk=record.service_id, user_id=order.user_id,
                domain_name=order.domain_name, package_id=plan["package"]["id"]).exists():
            raise ValidationError("The retained hosting association needs owner review.")
        if record.invoice_id and (order.invoice_id != record.invoice_id or record.invoice.user_id != order.user_id):
            raise ValidationError("The retained invoice association needs owner review.")
        return record
    if plan["invoice"] and order.invoice_id:
        raise ValidationError("An invoice already exists outside the saved choices. Review it before creating another.")
    service, invoice, job = None, None, None
    if plan["hosting"]:
        package = _package(plan)
        if Service.objects.filter(domain_name__iexact=order.domain_name).exists():
            raise ValidationError("Existing hosting for this domain needs review before another service is created.")
        service = Service.objects.create(user_id=order.user_id, package=package, domain_name=order.domain_name,
            status="pending", billing_period="annually", provisioning_provider=plan["package"]["provider"],
            notes=f"Saved manual domain order {order.pk}.")
        job = ProvisioningJob.objects.create(service=service, idempotency_key=f"manual-domain-followup-{record.pk}", status="queued")
    if plan["invoice"]:
        domain_price = Decimal(plan["order"]["total_price"])
        lines = [LineItemSpec(description=f"Domain registration: {order.domain_name} x {order.registration_years} year(s) (manual/cash order)",
            quantity=Decimal("1"), unit_price=Decimal(plan["override"]) if plan["override"] is not None and not service else domain_price, position=0)]
        if service:
            lines.append(LineItemSpec(description=f"Hosting: {plan['package']['name']} ({order.domain_name})",
                quantity=Decimal("1"), unit_price=Decimal(plan["package"]["annual_price"]), position=1))
            if plan["override"] is not None:
                lines = [LineItemSpec(description=f"Manual order: {order.domain_name} + {plan['package']['name']} x {order.registration_years} yr domain",
                    quantity=Decimal("1"), unit_price=Decimal(plan["override"]), position=0)]
        invoice = create_invoice(user=order.user, line_items=lines, source_kind=Invoice.SOURCE_MANUAL_ADMIN,
            status=Invoice.STATUS_UNPAID, notes=plan["notes"] or f"Saved manual/cash order {order.pk}.",
            created_by_staff=actor, send_email=False)
        order.invoice = invoice
        order.save(update_fields=["invoice", "updated_at"])
        if service:
            service.invoice = invoice
            service.save(update_fields=["invoice", "updated_at"])
    record.service, record.invoice, record.job, record.prepared_at = service, invoice, job, timezone.now()
    record.save(update_fields=["service", "invoice", "job", "prepared_at", "updated_at"])
    _audit(record, actor, "prepared")
    return record


def complete_manual_followup(*, order_id, actor):
    record = prepare_manual_followup(order_id=order_id, actor=actor)
    if record is None or not record.job_id:
        return record
    with transaction.atomic(durable=True):
        DomainOrder.objects.select_for_update().get(pk=order_id)
        record = ManualDomainFollowup.objects.select_for_update().get(pk=record.pk)
        actor = _actor(actor.pk, _intact(record))
        if record.dispatch_state == "dispatched" and (record.worker_started_at or record.job.status != "failed"):
            return record
        if record.worker_started_at or record.job.status not in {"queued", "failed"}:
            raise ValidationError("The hosting worker may already have started. Review the retained job before any provider retry.")
        if record.job.status == "failed":
            record.job.status, record.job.last_error = "queued", ""
            record.job.save(update_fields=["status", "last_error", "updated_at"])
        # Redelivery uses this same retained job. Its independent worker fence
        # admits at most one hosting mutation, including ambiguous broker acks.
        token = uuid.uuid4()
        record.dispatch_state, record.dispatch_actor = "submitting", actor
        record.dispatch_token = token
        record.save(update_fields=["dispatch_state", "dispatch_actor", "dispatch_token", "updated_at"])
        _audit(record, actor, "dispatch_started")
    try:
        from apps.provisioning.tasks import provision_hosting_account
        provision_hosting_account.apply_async(args=[record.service_id, record.job_id], task_id=f"manual-domain-followup-{record.pk}")
        with transaction.atomic(durable=True):
            record = ManualDomainFollowup.objects.select_for_update().get(pk=record.pk)
            if record.dispatch_token == token:
                record.dispatch_state = "dispatched"
                record.save(update_fields=["dispatch_state", "updated_at"])
                _audit(record, actor, "dispatched")
    except BaseException:
        with transaction.atomic(durable=True):
            record = ManualDomainFollowup.objects.select_for_update().get(pk=record.pk)
            if record.dispatch_token == token:
                record.dispatch_state = "unknown"
                record.save(update_fields=["dispatch_state", "updated_at"])
        raise ValidationError("Saved follow-up records are retained, but queue delivery was not confirmed. Reopen the saved follow-ups to retry delivery of this same job; a started worker requires review.") from None
    return record


def claim_manual_provisioning(*, service, job):
    """Fence duplicate delivery of these new jobs before a hosting mutation."""
    initial = (ManualDomainFollowup.objects.filter(service_id=service.pk).first()
        or ManualDomainFollowup.objects.filter(job_id=job.pk).first())
    if initial is None:
        return service, False
    return _claim_manual_provisioning(service=service, job=job, initial=initial)


@transaction.atomic(durable=True)
def _claim_manual_provisioning(*, service, job, initial):
    order = _locked_order(initial.order_id)
    record = ManualDomainFollowup.objects.select_for_update().get(pk=initial.pk)
    plan = _intact(record)
    if record.job_id != job.pk:
        raise ValidationError("Use the retained manual hosting job rather than another provisioning attempt.")
    if record.worker_started_at or job.status == "completed":
        return None, True
    if record.dispatch_state not in {"submitting", "dispatched", "unknown"} or not record.dispatch_actor_id:
        raise ValidationError("The saved hosting job has no delivery authorization.")
    actor = _actor(record.dispatch_actor_id, plan)
    _registered(order, plan)
    package = _package(plan)
    from apps.provisioning.whm_client import WHMClient
    client = WHMClient()
    if _whm_scope(client) != plan["package"]["whm_scope"]:
        raise ValidationError("The WHM account association changed before hosting admission.")
    current = Service.objects.select_for_update().get(pk=service.pk)
    current_job = ProvisioningJob.objects.select_for_update().get(pk=job.pk)
    if (record.service_id != current.pk or current_job.service_id != current.pk or current.user_id != order.user_id
            or current.domain_name != order.domain_name or current.package_id != package.pk
            or current.provisioning_provider != plan["package"]["provider"] or current.provisioning_config
            or current.status != "pending" or current_job.status != "queued"):
        raise ValidationError("The saved hosting association changed. Review it before provisioning.")
    record.worker_started_at = timezone.now()
    record.save(update_fields=["worker_started_at", "updated_at"])
    _audit(record, actor, "hosting_started")
    current.package = package
    current._manual_whm_client = client
    return current, True
