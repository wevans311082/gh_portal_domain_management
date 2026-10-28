import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.cloudflare_integration.models import CloudflareZone
from apps.cloudflare_integration.services import CloudflareService
from apps.dns.models import DNSRecord, DNSZone
from apps.domains.models import Domain, DomainOrder, DomainPricingSettings, DomainRenewal, DomainTransfer
from apps.domains.pricing import TLDPricingService
from apps.domains.resellerclub_client import ResellerClubClient
from apps.domains.services import DomainContactService

logger = logging.getLogger(__name__)

TLD_PRICING_SYNC_TASK_NAME = "Sync TLD pricing"
TLD_PRICING_SYNC_TASK_PATH = "apps.domains.tasks.sync_tld_pricing"

AUTO_RENEW_TASK_NAME = "Process auto-renewals"
AUTO_RENEW_TASK_PATH = "apps.domains.tasks.process_auto_renewals"


def ensure_auto_renew_schedule():
    """Register the daily process_auto_renewals beat task (idempotent)."""
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=24,
        period=IntervalSchedule.HOURS,
    )
    task, created = PeriodicTask.objects.update_or_create(
        name=AUTO_RENEW_TASK_NAME,
        defaults={
            "task": AUTO_RENEW_TASK_PATH,
            "interval": schedule,
            "enabled": True,
        },
    )
    logger.info(
        "%s auto-renew beat task: %s",
        "Registered" if created else "Updated",
        AUTO_RENEW_TASK_NAME,
    )
    return task


def ensure_tld_pricing_sync_schedule(settings_obj=None):
    settings_obj = settings_obj or DomainPricingSettings.get_solo()
    interval, _ = IntervalSchedule.objects.get_or_create(
        every=settings_obj.sync_interval_hours,
        period=IntervalSchedule.HOURS,
    )
    task, _ = PeriodicTask.objects.update_or_create(
        name=TLD_PRICING_SYNC_TASK_NAME,
        defaults={
            "task": TLD_PRICING_SYNC_TASK_PATH,
            "interval": interval,
            "enabled": settings_obj.sync_enabled,
        },
    )
    return task


def _ensure_cloudflare_zone(domain, order, registrar_client):
    zone_response = CloudflareService().create_zone(domain.name)
    zone_data = zone_response.get("result", {})
    zone_id = zone_data.get("id", "")
    assigned_nameservers = zone_data.get("name_servers", [])

    domain.cloudflare_zone_id = zone_id
    domain.save(update_fields=["cloudflare_zone_id", "updated_at"])

    CloudflareZone.objects.update_or_create(
        domain=domain,
        defaults={"zone_id": zone_id, "is_active": True},
    )
    zone, _ = DNSZone.objects.update_or_create(
        domain=domain,
        defaults={"provider": Domain.DNS_PROVIDER_CLOUDFLARE, "is_active": True, "last_synced": timezone.now()},
    )
    record_response = CloudflareService().create_dns_record(
        zone_id=zone_id,
        record_type="CNAME",
        name="www",
        content=settings.PLATFORM_WWW_TARGET,
        ttl=3600,
        proxied=True,
    )
    DNSRecord.objects.update_or_create(
        zone=zone,
        record_type=DNSRecord.TYPE_CNAME,
        name="www",
        defaults={
            "content": settings.PLATFORM_WWW_TARGET,
            "ttl": 3600,
            "proxied": True,
            "external_id": record_response.get("result", {}).get("id", ""),
            "is_active": True,
        },
    )
    if assigned_nameservers:
        registrar_client.modify_nameservers(order.registrar_order_id, assigned_nameservers)
        domain.nameserver1 = assigned_nameservers[0]
        domain.nameserver2 = assigned_nameservers[1] if len(assigned_nameservers) > 1 else ""
        domain.save(update_fields=["nameserver1", "nameserver2", "updated_at"])


def _normalize_ns_list(values) -> list[str]:
    """Normalize nameserver hostnames from env/runtime/API values."""
    if values is None:
        return []
    if isinstance(values, str):
        # Support "ns1.a.com,ns2.a.com" and whitespace/newline separated
        raw_parts = values.replace(";", ",").replace("\n", ",").split(",")
        values = raw_parts
    out: list[str] = []
    for item in values:
        host = str(item or "").strip().lower().rstrip(".")
        if host and host not in out:
            out.append(host)
    return out


def _configured_nameservers() -> list[str]:
    """Resolve platform nameservers from runtime DB settings, then env."""
    from apps.core.runtime_settings import get_runtime_list, get_runtime_setting

    # Runtime IntegrationSetting (Admin → Integrations) takes priority.
    runtime = get_runtime_list("WHM_NAMESERVERS", default=None)
    if runtime:
        cleaned = _normalize_ns_list(runtime)
        if cleaned:
            return cleaned

    # Single string runtime value
    runtime_str = get_runtime_setting("WHM_NAMESERVERS", "")
    cleaned = _normalize_ns_list(runtime_str)
    if cleaned:
        return cleaned

    return _normalize_ns_list(getattr(settings, "WHM_NAMESERVERS", []) or [])


def _build_nameservers(order):
    """Nameservers attached to a new registration.

    Sources (first match meeting the TLD minimum wins for non-Cloudflare):
    1. Order contact domain fields already set on a related domain (rare)
    2. WHM_NAMESERVERS from runtime settings / .env
    3. Live WHM nameserver config / hostname-derived ns1/ns2
    4. Cloudflare placeholder pair only when provider is Cloudflare
    """
    configured = _configured_nameservers()
    from apps.domains.registrar_policy import policy_for
    minimum = policy_for(order.tld).min_nameservers

    if order.dns_provider == Domain.DNS_PROVIDER_CLOUDFLARE:
        # Temporary NS for the initial register call; CF NS applied after zone create.
        return (configured[:2] or ["ns1.pending-cloudflare.invalid", "ns2.pending-cloudflare.invalid"])

    if len(configured) >= minimum:
        return configured

    # Fall back to WHM so registration still works when env var was never set
    # but WHM credentials are present (typical lab/prod gap).
    try:
        from apps.provisioning.whm_client import WHMClient

        whm_ns = _normalize_ns_list(WHMClient().get_nameservers())
        if len(whm_ns) >= minimum:
            logger.info("Using nameservers from WHM: %s", whm_ns)
            return whm_ns
        if whm_ns:
            logger.warning("WHM returned fewer than the required %s nameservers: %s", minimum, whm_ns)
    except Exception as exc:
        logger.warning("Could not load nameservers from WHM: %s", exc)

    return configured


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def sync_tld_pricing(self, tlds=None):
    settings_obj = DomainPricingSettings.get_solo()
    settings_obj.last_sync_started_at = timezone.now()
    settings_obj.last_sync_error = ""
    settings_obj.save(update_fields=["last_sync_started_at", "last_sync_error", "updated_at"])

    try:
        synced_records = TLDPricingService().sync_pricing(tlds=tlds)
    except Exception as exc:
        settings_obj.last_sync_error = str(exc)
        settings_obj.save(update_fields=["last_sync_error", "updated_at"])
        logger.exception("TLD pricing sync failed")
        raise self.retry(exc=exc)

    settings_obj.last_sync_completed_at = timezone.now()
    settings_obj.last_sync_error = ""
    settings_obj.save(update_fields=["last_sync_completed_at", "last_sync_error", "updated_at"])
    ensure_tld_pricing_sync_schedule(settings_obj)
    logger.info("Synced %s TLD pricing records", len(synced_records))
    return len(synced_records)


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def register_domain_order(self, order_id):
    order = DomainOrder.objects.select_related(
        "user",
        "invoice",
        "registration_contact",
        "admin_contact",
        "tech_contact",
        "billing_contact",
        "domain",
    ).get(id=order_id)

    if DomainOrder.objects.filter(pk=order.pk, registration_attempt__legacy_review_required=True).exists():
        from apps.domains.registration_submission import RegistrationReviewRequired
        raise RegistrationReviewRequired("Historical registration requires owner review before processing or local completion.")

    if order.status == DomainOrder.STATUS_COMPLETED and order.domain_id:
        return order.domain_id

    if order.status == DomainOrder.STATUS_PAUSED:
        raise ValueError("Domain order is paused and will not be registered until resumed.")
    if order.status == DomainOrder.STATUS_CANCELLED:
        raise ValueError("Domain order is cancelled.")

    # Paid invoice path (cart/checkout) OR staff manual order (no invoice, status paid).
    if order.invoice_id:
        if order.invoice.status != order.invoice.STATUS_PAID:
            raise ValueError("Domain order cannot be registered until the invoice is paid.")
    elif order.status not in (
        DomainOrder.STATUS_PAID,
        DomainOrder.STATUS_PROCESSING,
    ):
        raise ValueError(
            "Manual domain orders without an invoice must be marked paid before registration."
        )

    from apps.core.runtime_settings import get_runtime_setting
    from apps.domains.resellerclub_client import ResellerClubError

    acknowledgement = getattr(order, "registration_attempt", None)
    already_acknowledged = acknowledgement is not None and acknowledgement.status == "accepted" and not acknowledgement.legacy_review_required
    customer_id = (
        (acknowledgement.customer_id if already_acknowledged else "")
        or get_runtime_setting("RESELLERCLUB_CUSTOMER_ID", "")
        or getattr(settings, "RESELLERCLUB_CUSTOMER_ID", "")
        or ""
    ).strip()
    if not customer_id:
        order.status = DomainOrder.STATUS_FAILED
        order.last_error = "RESELLERCLUB_CUSTOMER_ID is not configured."
        order.save(update_fields=["status", "last_error", "updated_at"])
        raise ValueError(order.last_error)

    registrar_client = ResellerClubClient()
    nameservers = acknowledgement.subject.get("nameservers", []) if already_acknowledged else _build_nameservers(order)

    from apps.domains.registrar_policy import policy_for
    minimum_nameservers = policy_for(order.tld).min_nameservers
    if not already_acknowledged and len(nameservers) < minimum_nameservers:
        order.status = DomainOrder.STATUS_FAILED
        order.last_error = (
            f"At least {minimum_nameservers} nameserver(s) are required before registering this domain. "
            "Set WHM_NAMESERVERS in .env (comma-separated, e.g. ns1.example.com,ns2.example.com), "
            "or under Admin → Integrations / runtime settings, or ensure WHM API can return "
            "nameserver config (get_nameserver_config / gethostname)."
        )
        order.save(update_fields=["status", "last_error", "updated_at"])
        raise ValueError(order.last_error)

    order.status = DomainOrder.STATUS_PROCESSING
    order.last_error = ""
    order.save(update_fields=["status", "last_error", "updated_at"])

    try:
        from apps.domains.registration_submission import submit_registration
        registrar_order_id = submit_registration(order.pk, customer_id, nameservers, client=registrar_client)
        order.refresh_from_db()
        from apps.domains.models import DomainRegistrationAttempt
        from apps.domains.registration_submission import _same_order, RegistrationReviewRequired
        retained = DomainRegistrationAttempt.objects.get(order=order)
        registry_facts = None
        if retained.registry_status:
            from apps.domains.historical_registration import verified_existing_order
            try:
                registry_facts = verified_existing_order(
                    registrar_client.get_registration_reconciliation_details(registrar_order_id),
                    identifier=registrar_order_id, domain_name=order.domain_name, customer_id=retained.customer_id,
                )
                if any(registry_facts["role_ids"][key] != retained.role_ids.get(key) for key in ("reg", "admin", "tech", "billing")):
                    raise ValueError
            except Exception:
                raise RegistrationReviewRequired("The adopted provider order changed or could not be rechecked. Review its current state before local completion.") from None
        with transaction.atomic():
            order = DomainOrder.objects.select_for_update().get(pk=order.pk)
            accepted = DomainRegistrationAttempt.objects.select_for_update().get(order=order)
            if accepted.status != "accepted" or not _same_order(accepted, order) or order.registrar_order_id != accepted.remote_order_id:
                raise RegistrationReviewRequired("The local order changed after registrar acknowledgement. Reconcile it before local completion.")
            if order.status == "completed" and order.domain_id:
                return order.domain_id
            nameservers = registry_facts["nameservers"] if registry_facts else accepted.subject["nameservers"]
            registered_at = registry_facts["registered_at"] if registry_facts else accepted.completed_at.date()
            from dateutil.relativedelta import relativedelta

            expires_at = registry_facts["expires_at"] if registry_facts else registered_at + relativedelta(years=max(1, int(order.registration_years or 1)))
            domain_defaults = {
                    "user": order.user,
                    "tld": order.tld,
                    "status": registry_facts["status"] if registry_facts else Domain.STATUS_ACTIVE,
                    "registrar_id": registrar_order_id,
                    "registered_at": registered_at,
                    "expires_at": expires_at,
                    "auto_renew": order.auto_renew,
                    "dns_provider": order.dns_provider,
                    "nameserver1": nameservers[0] if nameservers else "",
                    "nameserver2": nameservers[1] if len(nameservers) > 1 else "",
                    "nameserver3": nameservers[2] if len(nameservers) > 2 else "",
                    "nameserver4": nameservers[3] if len(nameservers) > 3 else "",
                }
            domain, created = Domain.objects.select_for_update().get_or_create(name=order.domain_name, defaults=domain_defaults)
            if not created:
                if domain.user_id != order.user_id or domain.registrar_id != registrar_order_id:
                    raise RegistrationReviewRequired("The local domain record conflicts with the acknowledged registrar order. Reconciliation is required.")
                if registry_facts:
                    for field in ("status", "registered_at", "expires_at", "nameserver1", "nameserver2", "nameserver3", "nameserver4"):
                        setattr(domain, field, domain_defaults[field])
                    domain.save(update_fields=["status", "registered_at", "expires_at", "nameserver1", "nameserver2", "nameserver3", "nameserver4", "updated_at"])
            if registry_facts:
                accepted.registry_status = registry_facts["status"]
                accepted.registry_checked_at = registry_facts["checked_at"]
                accepted.registry_registered_at = registered_at
                accepted.registry_expires_at = expires_at
                accepted.subject = {**accepted.subject, "nameservers": nameservers}
                accepted.save(update_fields=["registry_status", "registry_checked_at", "registry_registered_at", "registry_expires_at", "subject", "updated_at"])
            order.domain = domain
            order.registrar_order_id = registrar_order_id

            order.save(update_fields=["domain", "updated_at"])

        if registry_facts and registry_facts["status"] != "active":
            DNSZone.objects.update_or_create(
                domain=domain, defaults={"provider": order.dns_provider, "is_active": False, "last_synced": timezone.now()},
            )
        elif order.dns_provider == Domain.DNS_PROVIDER_CLOUDFLARE and settings.PLATFORM_WWW_TARGET:
            _ensure_cloudflare_zone(domain, order, registrar_client)
        else:
            DNSZone.objects.update_or_create(
                domain=domain,
                defaults={"provider": order.dns_provider, "is_active": True, "last_synced": timezone.now()},
            )

        with transaction.atomic():
            current = DomainOrder.objects.select_for_update().get(pk=order.pk)
            current_domain = Domain.objects.select_for_update().get(pk=domain.pk)
            if not _same_order(accepted, current) or current.registrar_order_id != registrar_order_id or current_domain.user_id != current.user_id or current_domain.registrar_id != registrar_order_id:
                raise RegistrationReviewRequired("The local domain or order changed during completion. Reconciliation is required.")
            current.status = DomainOrder.STATUS_COMPLETED
            current.completed_at = timezone.now()
            current.save(update_fields=["status", "completed_at", "updated_at"])
        logger.info("Registered domain order %s as domain %s", order.id, domain.name)
        return domain.id

    except Exception as exc:
        from apps.domains.registration_submission import RegistrationInProgress, RegistrationReviewRequired
        from django.core.exceptions import ValidationError
        if isinstance(exc, RegistrationInProgress):
            raise
        order.refresh_from_db()
        order.status = DomainOrder.STATUS_FAILED
        order.last_error = (
            "Registration requires review. Check the registrar submission state before retrying."
            if getattr(order, "registration_attempt", None) and order.registration_attempt.status in {"submitting", "unknown"}
            else "Registration stopped. Review the contact details, payment and registrar configuration before retrying."
        )
        if isinstance(exc, ValidationError):
            # These are local, authored validation messages, never a provider
            # response body. Keep actionable field guidance for staff review.
            order.last_error = " ".join(exc.messages)[:2000]
        order.save(update_fields=["status", "last_error", "updated_at"])
        logger.error("Domain order registration failed for order %s (%s)", order.id, type(exc).__name__)
        attempt = getattr(order, "registration_attempt", None)
        if attempt and attempt.status in {"submitting", "unknown"}:
            raise RegistrationReviewRequired("The registrar submission outcome requires reconciliation; no automatic retry was made.") from None
        # Do not Celery-retry permanent API / validation failures (HTTP 500 payload issues).
        permanent = isinstance(exc, (ResellerClubError, ValueError, ValidationError))
        msg = str(exc).lower()
        if permanent or "http 500" in msg or "too many 500" in msg or "invalid" in msg:
            raise
        raise self.retry(exc=exc)


@shared_task
def send_domain_expiry_reminders(days_before=30):
    target_date = timezone.now().date() + timedelta(days=days_before)
    domains = Domain.objects.select_related("user").filter(
        status=Domain.STATUS_ACTIVE,
        expires_at=target_date,
    )
    sent = 0
    if not domains.exists():
        return sent

    from apps.notifications.services import send_notification

    for domain in domains:
        if send_notification(
            template_name="domain_expiry_reminder",
            user=domain.user,
            context={"domain": domain.name, "expires_at": domain.expires_at, "days_before": days_before},
        ):
            sent += 1
    logger.info("Sent %s expiry reminder(s) for domains expiring in %s days", sent, days_before)
    return sent


@shared_task
def sync_domain_expiry_statuses():
    today = timezone.now().date()
    expired = Domain.objects.filter(status=Domain.STATUS_ACTIVE, expires_at__lt=today)
    updated = expired.update(status=Domain.STATUS_EXPIRED)
    logger.info("Marked %s domains as expired based on local expiry dates", updated)
    return updated


@shared_task
def execute_domain_renewal(renewal_id: int):
    """
    Execute a paid domain renewal via the registrar.

    Idempotent: if the renewal is already completed it exits early.
    On failure the renewal is marked FAILED — an admin can re-queue via
    the admin action or the auto-renew beat task.
    """
    try:
        renewal = DomainRenewal.objects.select_related("domain", "invoice").get(pk=renewal_id)
    except DomainRenewal.DoesNotExist:
        logger.error("execute_domain_renewal: DomainRenewal %s not found", renewal_id)
        return

    if renewal.status == DomainRenewal.STATUS_COMPLETED:
        logger.info("execute_domain_renewal: renewal %s already completed, skipping", renewal_id)
        return

    if renewal.invoice_id and renewal.invoice.status != renewal.invoice.STATUS_PAID:
        renewal.status = DomainRenewal.STATUS_FAILED
        renewal.last_error = "Invoice is not paid."
        renewal.save(update_fields=["status", "last_error"])
        logger.error("execute_domain_renewal: renewal %s invoice is not paid", renewal_id)
        return

    domain = renewal.domain
    from apps.domains.registrar_review import registrar_review_required, REVIEW_MESSAGE
    if registrar_review_required(domain):
        renewal.status = DomainRenewal.STATUS_FAILED
        renewal.last_error = REVIEW_MESSAGE
        renewal.save(update_fields=["status", "last_error", "updated_at"])
        return

    from apps.domains.registry_requirements import validate_renewal_registrant
    from django.core.exceptions import ValidationError
    try:
        validate_renewal_registrant(domain)
    except ValidationError as exc:
        renewal.status, renewal.last_error = DomainRenewal.STATUS_FAILED, " ".join(exc.messages)
        renewal.save(update_fields=["status", "last_error", "updated_at"])
        return

    if not domain.registrar_id:
        logger.error("execute_domain_renewal: domain %s has no registrar_id", domain.name)
        renewal.status = DomainRenewal.STATUS_FAILED
        renewal.last_error = "Domain has no registrar order ID — cannot renew."
        renewal.save(update_fields=["status", "last_error"])
        return

    renewal.status = DomainRenewal.STATUS_PROCESSING
    renewal.save(update_fields=["status"])

    try:
        client = ResellerClubClient()

        # ResellerClub expects the expiry timestamp as a Unix epoch integer
        import calendar
        current_expiry_epoch = (
            calendar.timegm(domain.expires_at.timetuple()) if domain.expires_at else 0
        )

        result = client.renew_domain(
            order_id=domain.registrar_id,
            years=renewal.renewal_years,
            current_expiry_date=current_expiry_epoch,
            auto_renew=domain.auto_renew,
        )

        # Update the domain's expiry date (+years)
        from dateutil.relativedelta import relativedelta
        new_expiry = (domain.expires_at or timezone.now().date()) + relativedelta(years=renewal.renewal_years)
        domain.expires_at = new_expiry
        domain.status = Domain.STATUS_ACTIVE
        domain.save(update_fields=["expires_at", "status"])

        renewal.status = DomainRenewal.STATUS_COMPLETED
        renewal.new_expiry_date = new_expiry
        renewal.completed_at = timezone.now()
        renewal.last_error = ""
        renewal.save(update_fields=["status", "new_expiry_date", "completed_at", "last_error"])

        logger.info(
            "execute_domain_renewal: domain %s renewed for %s year(s), new expiry %s (registrar result: %s)",
            domain.name,
            renewal.renewal_years,
            new_expiry,
            result,
        )

    except Exception as exc:
        logger.error("execute_domain_renewal: renewal %s failed: %s", renewal_id, exc)
        renewal.status = DomainRenewal.STATUS_FAILED
        renewal.last_error = str(exc)
        renewal.save(update_fields=["status", "last_error"])


@shared_task
def execute_domain_transfer(transfer_id: int):
    """Retain paid acknowledgement before local handoff; never repeat uncertainty."""
    from django.core.exceptions import ValidationError
    from apps.domains.models import DomainTransferAttempt
    from apps.domains.transfer_submission import (
        submit_transfer, TransferReviewRequired, TransferInProgress, _same_transfer,
    )
    from apps.domains.contact_bindings import provider_scope
    from apps.audit.models import AuditLog

    try:
        transfer = DomainTransfer.objects.select_related("submission_attempt").get(pk=transfer_id)
    except DomainTransfer.DoesNotExist:
        logger.warning("Transfer record %s was not found", transfer_id)
        return
    attempt = getattr(transfer, "submission_attempt", None)
    if transfer.status == "completed" and attempt is None:
        # Migration separately quarantines inconsistent historical completions.
        return transfer.domain_id
    try:
        if attempt and (attempt.legacy_review_required or attempt.status in {"unknown", "submitting"}):
            raise TransferReviewRequired("An earlier transfer may have reached the registrar. Owner review is required before continuing.")
        if attempt and attempt.status == "preparing":
            raise TransferInProgress("Another worker is preparing this transfer. Do not submit it again.")
        customer_id = attempt.customer_id if attempt and attempt.status == "accepted" else str(settings.RESELLERCLUB_CUSTOMER_ID or "").strip()
        if not customer_id:
            raise ValidationError("A registrar customer account must be configured before transfer.")
        nameservers = attempt.subject["nameservers"] if attempt and attempt.status == "accepted" else _build_nameservers(
            type("NSOrder", (), {"dns_provider": transfer.dns_provider})())
        client = ResellerClubClient()
        identifier = submit_transfer(transfer_id, customer_id, nameservers, client=client)
        with transaction.atomic(durable=True):
            current = DomainTransfer.objects.select_for_update().get(pk=transfer_id)
            retained = DomainTransferAttempt.objects.select_for_update().get(transfer=current)
            if (retained.status != "accepted" or not _same_transfer(retained, current)
                    or retained.provider_scope != provider_scope(client)):
                raise TransferReviewRequired("The acknowledged transfer association changed. Owner review is required.")
            domain, created = Domain.objects.select_for_update().get_or_create(name=current.domain_name, defaults={
                "user_id": current.user_id, "tld": current.tld, "status": Domain.STATUS_PENDING,
                "registrar_id": identifier, "auto_renew": current.auto_renew, "dns_provider": current.dns_provider,
                "nameserver1": nameservers[0] if nameservers else "", "nameserver2": nameservers[1] if len(nameservers) > 1 else "",
                "nameserver3": nameservers[2] if len(nameservers) > 2 else "",
                "nameserver4": nameservers[3] if len(nameservers) > 3 else "",
            })
            if domain.user_id != current.user_id or domain.registrar_id != identifier or (current.domain_id and current.domain_id != domain.pk):
                raise TransferReviewRequired("The local domain conflicts with this acknowledged transfer. Owner review is required.")
            if current.domain_id != domain.pk:
                current.domain = domain
                # A transfer order acknowledgement is not registry completion.
                current.status = DomainTransfer.STATUS_PROCESSING
                current.last_error = ""
                current.save(update_fields=["domain", "status", "last_error", "updated_at"])
                AuditLog.objects.create(action="domain.transfer.local_handoff", model_name="DomainTransfer",
                                        object_id=str(current.pk), data={"transfer_attempt_id": retained.pk, "domain_id": domain.pk})
            return domain.pk
    except TransferInProgress:
        return  # The active worker owns progress and its error/status fields.
    except Exception as exc:
        message = (" ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                   if isinstance(exc, TransferReviewRequired) else
                   "Transfer processing stopped. Review the retained submission state before continuing.")
        DomainTransfer.objects.filter(pk=transfer_id).exclude(status=DomainTransfer.STATUS_COMPLETED).update(
            last_error=message, status=DomainTransfer.STATUS_FAILED, updated_at=timezone.now())
        logger.warning("Transfer %s stopped; inspect its retained submission state", transfer_id)


@shared_task
def process_auto_renewals(days_ahead: int = 7):
    """
    Beat task: find active domains with auto_renew=True expiring within *days_ahead*
    days that don't already have a pending/paid/processing renewal, then create an
    invoice + DomainRenewal record and fire execute_domain_renewal via the normal
    Stripe webhook path.

    Creates an unpaid invoice so the customer can pay via Stripe. Registrar
    renewal runs from the normal paid-invoice webhook — this task never marks
    invoices paid without a charge.
    """
    from decimal import Decimal
    from apps.billing.models import Invoice, InvoiceLineItem
    from django.utils import timezone as tz

    today = tz.now().date()
    cutoff = today + timedelta(days=days_ahead)

    domains = Domain.objects.select_related("user").filter(
        status=Domain.STATUS_ACTIVE,
        auto_renew=True,
        expires_at__range=(today, cutoff),
    )

    queued = 0
    domains = domains.exclude(order__registration_attempt__legacy_review_required=True)
    for domain in domains:
        from apps.domains.registrar_review import registrar_review_required
        if registrar_review_required(domain):
            continue
        from apps.domains.registry_requirements import validate_renewal_registrant
        from django.core.exceptions import ValidationError
        try:
            validate_renewal_registrant(domain)
        except ValidationError:
            logger.info("Auto-renewal domain %s needs a registrar eligibility review", domain.pk)
            continue
        # Skip if a non-failed renewal already exists
        has_open_renewal = DomainRenewal.objects.filter(
            domain=domain,
            status__in=[
                DomainRenewal.STATUS_PENDING_PAYMENT,
                DomainRenewal.STATUS_PAID,
                DomainRenewal.STATUS_PROCESSING,
            ],
        ).exists()
        if has_open_renewal:
            continue

        pricing = None
        try:
            from apps.domains.models import TLDPricing
            pricing = TLDPricing.objects.get(tld=domain.tld, is_active=True)
        except Exception:
            logger.warning("process_auto_renewals: no pricing for .%s, skipping %s", domain.tld, domain.name)
            continue

        renewal_years = 1
        renewal_price = (pricing.renewal_price * Decimal(str(renewal_years))).quantize(Decimal("0.01"))

        # Build invoice via the canonical billing service so numbering,
        # branding, and audit stay consistent with manual renewals.
        from apps.billing.services import create_invoice

        invoice = create_invoice(
            user=domain.user,
            line_items=[{
                "description": f"Auto-renewal: {domain.name} (1 year)",
                "quantity": 1,
                "unit_price": renewal_price,
            }],
            source_kind=Invoice.SOURCE_AUTO_RENEWAL,
            vat_rate=Decimal("0.00"),
            due_date=today,
        )

        DomainRenewal.objects.create(
            domain=domain,
            user=domain.user,
            invoice=invoice,
            renewal_years=renewal_years,
            total_price=renewal_price,
            status=DomainRenewal.STATUS_PENDING_PAYMENT,
        )
        queued += 1
        logger.info("process_auto_renewals: queued renewal for %s (expiry %s)", domain.name, domain.expires_at)

    logger.info("process_auto_renewals: queued %s renewal(s)", queued)
    return queued


# ---------------------------------------------------------------------------
# Phase 9: Registrar balance monitor
# ---------------------------------------------------------------------------

REGISTRAR_BALANCE_TASK_NAME = "Monitor registrar balance"
REGISTRAR_BALANCE_TASK_PATH = "apps.domains.tasks.monitor_registrar_balance"


def ensure_registrar_balance_schedule():
    """Register a daily beat task to check registrar account balance (idempotent)."""
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=24,
        period=IntervalSchedule.HOURS,
    )
    task, created = PeriodicTask.objects.update_or_create(
        name=REGISTRAR_BALANCE_TASK_NAME,
        defaults={
            "task": REGISTRAR_BALANCE_TASK_PATH,
            "interval": schedule,
            "enabled": True,
        },
    )
    action = "Created" if created else "Updated"
    logger.info("%s beat task: %s", action, REGISTRAR_BALANCE_TASK_NAME)
    return task


@shared_task(name="domains.monitor_registrar_balance")
def monitor_registrar_balance():
    """
    Beat task: check ResellerClub account balance and alert staff if it falls
    below the threshold set in ``REGISTRAR_LOW_BALANCE_THRESHOLD`` (default 50.00).
    """
    from decimal import Decimal
    from apps.core.runtime_settings import get_runtime_setting
    from apps.notifications.services import send_notification

    threshold = Decimal(str(get_runtime_setting("REGISTRAR_LOW_BALANCE_THRESHOLD", "50.00")))
    client = ResellerClubClient()

    try:
        result = client._get("accounts/details/", params={"no-of-records": 1, "page-no": 1})
        balance_raw = result.get("sellingcurrencybalance") or result.get("currentaccountbalance") or ""
        balance = Decimal(str(balance_raw)) if balance_raw else None
    except Exception as exc:
        logger.error("monitor_registrar_balance: failed to fetch balance: %s", exc)
        return {"error": str(exc)}

    if balance is None:
        logger.warning("monitor_registrar_balance: could not parse balance from response")
        return {"balance": None}

    logger.info("monitor_registrar_balance: balance=%.2f threshold=%.2f", balance, threshold)

    if balance < threshold:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        staff_emails = list(User.objects.filter(is_staff=True, is_active=True).values_list("email", flat=True))
        for email in staff_emails:
            try:
                delivered = send_notification(
                    "registrar_low_balance",
                    None,
                    {"balance": balance, "threshold": threshold},
                    recipient_email=email,
                )
                if not delivered:
                    logger.warning("monitor_registrar_balance: notification was not delivered to %s", email)
            except Exception as exc:
                logger.warning("monitor_registrar_balance: notification failed for %s: %s", email, exc)
        logger.warning(
            "monitor_registrar_balance: LOW BALANCE alert — balance=%.2f < threshold=%.2f",
            balance,
            threshold,
        )

    return {"balance": float(balance), "threshold": float(threshold), "alert_sent": balance < threshold}


# Register the durable verification workers with Celery's standard task discovery.
from apps.domains.verification_actions import dispatch_verification_actions, run_verification_action, schedule_verification_checks  # noqa: E402,F401
from apps.domains.contact_change_jobs import dispatch_contact_changes, run_contact_change  # noqa: E402,F401
from apps.domains.publication_redaction_jobs import dispatch_publication_redactions, run_publication_redaction  # noqa: E402,F401
