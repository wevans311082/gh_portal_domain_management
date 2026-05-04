import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
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


def _build_nameservers(order):
    if order.dns_provider == Domain.DNS_PROVIDER_CLOUDFLARE:
        return list(settings.WHM_NAMESERVERS)[:2] or ["ns1.pending-cloudflare.invalid", "ns2.pending-cloudflare.invalid"]
    return list(settings.WHM_NAMESERVERS)


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

    if order.status == DomainOrder.STATUS_COMPLETED and order.domain_id:
        return order.domain_id

    if not order.invoice or order.invoice.status != order.invoice.STATUS_PAID:
        raise ValueError("Domain order cannot be registered until the invoice is paid.")

    if not settings.RESELLERCLUB_CUSTOMER_ID:
        order.status = DomainOrder.STATUS_FAILED
        order.last_error = "RESELLERCLUB_CUSTOMER_ID is not configured."
        order.save(update_fields=["status", "last_error", "updated_at"])
        raise ValueError(order.last_error)

    registrar_client = ResellerClubClient()
    contact_service = DomainContactService(client=registrar_client)
    nameservers = _build_nameservers(order)

    if not nameservers:
        order.status = DomainOrder.STATUS_FAILED
        order.last_error = "WHM_NAMESERVERS must be configured before registering domains."
        order.save(update_fields=["status", "last_error", "updated_at"])
        raise ValueError(order.last_error)

    order.status = DomainOrder.STATUS_PROCESSING
    order.last_error = ""
    order.save(update_fields=["status", "last_error", "updated_at"])

    try:
        registration_contact_id = contact_service.sync_remote_contact(order.registration_contact, settings.RESELLERCLUB_CUSTOMER_ID)
        admin_contact_id = contact_service.sync_remote_contact(order.admin_contact, settings.RESELLERCLUB_CUSTOMER_ID)
        tech_contact_id = contact_service.sync_remote_contact(order.tech_contact, settings.RESELLERCLUB_CUSTOMER_ID)
        billing_contact_id = contact_service.sync_remote_contact(order.billing_contact, settings.RESELLERCLUB_CUSTOMER_ID)

        registration_response = registrar_client.register_domain(
            domain_name=order.domain_name,
            years=order.registration_years,
            customer_id=settings.RESELLERCLUB_CUSTOMER_ID,
            reg_contact_id=registration_contact_id,
            admin_contact_id=admin_contact_id,
            tech_contact_id=tech_contact_id,
            billing_contact_id=billing_contact_id,
            nameservers=nameservers,
            purchase_privacy=order.privacy_enabled,
            auto_renew=order.auto_renew,
        )
        registrar_order_id = str(
            registration_response.get("entityid")
            or registration_response.get("orderid")
            or registration_response.get("order-id")
            or ""
        )
        domain, _ = Domain.objects.update_or_create(
            name=order.domain_name,
            defaults={
                "user": order.user,
                "tld": order.tld,
                "status": Domain.STATUS_ACTIVE,
                "registrar_id": registrar_order_id,
                "registered_at": timezone.now().date(),
                "auto_renew": order.auto_renew,
                "dns_provider": order.dns_provider,
                "nameserver1": nameservers[0] if nameservers else "",
                "nameserver2": nameservers[1] if len(nameservers) > 1 else "",
            },
        )
        order.domain = domain
        order.registrar_order_id = registrar_order_id

        if order.dns_provider == Domain.DNS_PROVIDER_CLOUDFLARE and settings.PLATFORM_WWW_TARGET:
            _ensure_cloudflare_zone(domain, order, registrar_client)
        else:
            DNSZone.objects.update_or_create(
                domain=domain,
                defaults={"provider": order.dns_provider, "is_active": True, "last_synced": timezone.now()},
            )

        order.status = DomainOrder.STATUS_COMPLETED
        order.completed_at = timezone.now()
        order.save(update_fields=["domain", "registrar_order_id", "status", "completed_at", "updated_at"])
        logger.info("Registered domain order %s as domain %s", order.id, domain.name)
        return domain.id
    except Exception as exc:
        order.status = DomainOrder.STATUS_FAILED
        order.last_error = str(exc)
        order.save(update_fields=["status", "last_error", "updated_at"])
        logger.exception("Domain order registration failed for order %s", order.id)
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
        send_notification(
            template_name="domain_expiry_reminder",
            user=domain.user,
            context={"domain": domain.name, "expires_at": domain.expires_at, "days_before": days_before},
        )
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

    domain = renewal.domain

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
    """Execute a paid domain transfer via the registrar."""
    try:
        transfer = DomainTransfer.objects.select_related(
            "invoice",
            "registration_contact",
            "admin_contact",
            "tech_contact",
            "billing_contact",
            "domain",
        ).get(pk=transfer_id)
    except DomainTransfer.DoesNotExist:
        logger.error("execute_domain_transfer: DomainTransfer %s not found", transfer_id)
        return

    if transfer.status == DomainTransfer.STATUS_COMPLETED:
        logger.info("execute_domain_transfer: transfer %s already completed, skipping", transfer_id)
        return

    if not settings.RESELLERCLUB_CUSTOMER_ID:
        transfer.status = DomainTransfer.STATUS_FAILED
        transfer.last_error = "RESELLERCLUB_CUSTOMER_ID is not configured."
        transfer.save(update_fields=["status", "last_error"])
        return

    registrar_client = ResellerClubClient()
    contact_service = DomainContactService(client=registrar_client)
    nameservers = list(settings.WHM_NAMESERVERS)
    if not nameservers:
        transfer.status = DomainTransfer.STATUS_FAILED
        transfer.last_error = "WHM_NAMESERVERS must be configured before transferring domains."
        transfer.save(update_fields=["status", "last_error"])
        return

    transfer.status = DomainTransfer.STATUS_PROCESSING
    transfer.last_error = ""
    transfer.save(update_fields=["status", "last_error"])

    try:
        registration_contact_id = contact_service.sync_remote_contact(transfer.registration_contact, settings.RESELLERCLUB_CUSTOMER_ID)
        admin_contact_id = contact_service.sync_remote_contact(transfer.admin_contact, settings.RESELLERCLUB_CUSTOMER_ID)
        tech_contact_id = contact_service.sync_remote_contact(transfer.tech_contact, settings.RESELLERCLUB_CUSTOMER_ID)
        billing_contact_id = contact_service.sync_remote_contact(transfer.billing_contact, settings.RESELLERCLUB_CUSTOMER_ID)

        transfer_response = registrar_client.transfer_domain(
            domain_name=transfer.domain_name,
            customer_id=settings.RESELLERCLUB_CUSTOMER_ID,
            reg_contact_id=registration_contact_id,
            admin_contact_id=admin_contact_id,
            tech_contact_id=tech_contact_id,
            billing_contact_id=billing_contact_id,
            nameservers=nameservers,
            auth_code=transfer.auth_code,
            auto_renew=transfer.auto_renew,
        )
        registrar_order_id = str(
            transfer_response.get("entityid")
            or transfer_response.get("orderid")
            or transfer_response.get("order-id")
            or ""
        )

        domain, _ = Domain.objects.update_or_create(
            name=transfer.domain_name,
            defaults={
                "user": transfer.user,
                "tld": transfer.tld,
                "status": Domain.STATUS_PENDING,
                "registrar_id": registrar_order_id,
                "auto_renew": transfer.auto_renew,
                "dns_provider": transfer.dns_provider,
                "nameserver1": nameservers[0] if nameservers else "",
                "nameserver2": nameservers[1] if len(nameservers) > 1 else "",
            },
        )
        transfer.domain = domain
        transfer.registrar_order_id = registrar_order_id
        transfer.status = DomainTransfer.STATUS_COMPLETED
        transfer.completed_at = timezone.now()
        transfer.save(update_fields=["domain", "registrar_order_id", "status", "completed_at", "updated_at"])
        logger.info("Transferred domain %s into local account", transfer.domain_name)
        return domain.id
    except Exception as exc:
        transfer.status = DomainTransfer.STATUS_FAILED
        transfer.last_error = str(exc)
        transfer.save(update_fields=["status", "last_error", "updated_at"])
        logger.exception("Domain transfer failed for %s", transfer.domain_name)


@shared_task
def process_auto_renewals(days_ahead: int = 7):
    """
    Beat task: find active domains with auto_renew=True expiring within *days_ahead*
    days that don't already have a pending/paid/processing renewal, then create an
    invoice + DomainRenewal record and fire execute_domain_renewal via the normal
    Stripe webhook path.

    Because the invoice starts as UNPAID we also immediately mark it PAID here
    (auto-renew means the card on file should be charged separately; this task
    handles registrar-side renewal for managed accounts).  If you integrate
    Stripe billing, remove the direct status flip and let the webhook handle it.
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
    for domain in domains:
        # Skip if a non-failed renewal already exists
        has_open_renewal = DomainRenewal.objects.filter(
            domain=domain,
            status__in=[
                DomainRenewal.STATUS_PENDING_PAYMENT,
                DomainRenewal.STATUS_PAID,
                DomainRenewal.STATUS_PROCESSING,
                DomainRenewal.STATUS_COMPLETED,
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
            status=Invoice.STATUS_PAID,
        )

        renewal = DomainRenewal.objects.create(
            domain=domain,
            user=domain.user,
            invoice=invoice,
            renewal_years=renewal_years,
            total_price=renewal_price,
            status=DomainRenewal.STATUS_PAID,
        )

        execute_domain_renewal.delay(renewal.id)
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
                send_notification(
                    template_name="registrar_low_balance",
                    recipient_email=email,
                    context={"balance": balance, "threshold": threshold},
                )
            except Exception as exc:
                logger.warning("monitor_registrar_balance: notification failed for %s: %s", email, exc)
        logger.warning(
            "monitor_registrar_balance: LOW BALANCE alert — balance=%.2f < threshold=%.2f",
            balance,
            threshold,
        )

    return {"balance": float(balance), "threshold": float(threshold), "alert_sent": balance < threshold}
