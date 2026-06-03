"""Celery tasks for hosting provisioning."""
import logging
import uuid
from django.utils import timezone
from django.db import transaction
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

from apps.services.models import Service
from apps.provisioning.models import ProvisioningJob
from apps.provisioning.providers import ProvisioningProviderError, get_provider_for_service
from apps.provisioning.whm_client import WHMClient, WHMClientError, generate_cpanel_username, generate_secure_password

logger = logging.getLogger(__name__)

WHM_SYNC_TASK_NAME = "Sync WHM inventory"
WHM_SYNC_TASK_PATH = "apps.provisioning.tasks.sync_whm_inventory"
WHM_RECONCILE_TASK_NAME = "Cross-check WHM domains with ResellerClub"
WHM_RECONCILE_TASK_PATH = "apps.provisioning.tasks.reconcile_whm_registrar_domains"


def ensure_whm_sync_schedule():
    """Register a recurring WHM inventory sync task (idempotent)."""
    from django_celery_beat.models import IntervalSchedule, PeriodicTask

    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=15,
        period=IntervalSchedule.MINUTES,
    )
    task, created = PeriodicTask.objects.update_or_create(
        name=WHM_SYNC_TASK_NAME,
        defaults={"task": WHM_SYNC_TASK_PATH, "interval": schedule, "enabled": True},
    )
    logger.info("%s beat task: %s", "Registered" if created else "Updated", WHM_SYNC_TASK_NAME)
    return task


@shared_task(name="apps.provisioning.tasks.sync_whm_inventory")
def sync_whm_inventory():
    """Fetch and persist WHM accounts/packages/usage snapshots."""
    from apps.provisioning.whm_sync import WHMSyncService

    result = WHMSyncService().sync_all()
    logger.info(
        "WHM sync complete run=%s packages=%s accounts=%s usage=%s errors=%s",
        result.get("sync_run_id"),
        result.get("package_count"),
        result.get("account_count"),
        result.get("usage_count"),
        result.get("error_count"),
    )
    return result


@shared_task(name="apps.provisioning.tasks.reconcile_whm_registrar_domains")
def reconcile_whm_registrar_domains():
    """Refresh WHM inventory, compare against ResellerClub, and store report."""
    from apps.provisioning.models import WHMSyncRun
    from apps.provisioning.whm_sync import WHMSyncService

    service = WHMSyncService()
    sync_result = service.sync_all()
    report = service.build_domain_reconciliation()
    serialized_report = service.serialize_domain_reconciliation(report)

    sync_run_id = sync_result.get("sync_run_id")
    if sync_run_id:
        sync_run = WHMSyncRun.objects.filter(pk=sync_run_id).first()
        if sync_run:
            result_data = dict(sync_run.result_data or {})
            result_data["domain_reconciliation"] = serialized_report
            sync_run.result_data = result_data
            sync_run.save(update_fields=["result_data", "updated_at"])

    logger.info(
        "WHM registrar reconciliation complete run=%s orphaned=%s registrar_only=%s",
        sync_run_id,
        serialized_report.get("orphaned_account_total"),
        serialized_report.get("registrar_only_domain_total"),
    )
    return {
        "ok": True,
        "sync_run_id": sync_run_id,
        "orphaned_account_total": serialized_report.get("orphaned_account_total", 0),
        "registrar_only_domain_total": serialized_report.get("registrar_only_domain_total", 0),
    }


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def provision_hosting_account(self, service_id: int, job_id: int):
    """
    Provision a cPanel hosting account for a service.
    Idempotent - checks if already provisioned before proceeding.
    """
    try:
        service = Service.objects.select_related("user", "package").get(id=service_id)
        job = ProvisioningJob.objects.get(id=job_id)
    except (Service.DoesNotExist, ProvisioningJob.DoesNotExist) as e:
        logger.error(f"Cannot find service or job: {e}")
        return

    # Idempotency check
    if job.status == ProvisioningJob.STATUS_COMPLETED:
        logger.info(f"Provisioning job {job_id} already completed, skipping.")
        return

    if job.attempt_count >= job.max_attempts:
        job.status = ProvisioningJob.STATUS_FAILED
        job.save(update_fields=["status"])
        service.status = Service.STATUS_FAILED
        service.save(update_fields=["status"])
        logger.error(f"Provisioning job {job_id} exceeded max attempts.")
        return

    job.status = ProvisioningJob.STATUS_IN_PROGRESS
    job.attempt_count += 1
    job.celery_task_id = self.request.id or ""
    job.save(update_fields=["status", "attempt_count", "celery_task_id"])

    try:
        provider = get_provider_for_service(service)
        username = generate_cpanel_username(service.domain_name or service.user.email.split("@")[0])
        password = generate_secure_password()

        result = provider.create_site(
            domain=service.domain_name,
            username=username,
            password=password,
            package=service.package.whm_package_name,
            email=service.user.email,
            service=service,
        )

        # Wrap the database updates in a transaction so that if any save fails
        # after the WHM account has been created we still have a consistent
        # record — rather than a provisioned server account with no local record.
        with transaction.atomic():
            service.status = Service.STATUS_ACTIVE
            service.cpanel_username = username
            service.save(update_fields=["status", "cpanel_username"])

            job.status = ProvisioningJob.STATUS_COMPLETED
            job.completed_at = timezone.now()
            job.result_data = {
                "username": username,
                "provider": provider.provider_key,
                "result": str(result),
            }
            job.save(update_fields=["status", "completed_at", "result_data"])

        # Import here to avoid circular imports
        from apps.notifications.services import send_notification
        send_notification(
            template_name="hosting_provisioned",
            user=service.user,
            context={
                "service": service,
                "username": username,
                "domain": service.domain_name,
                "package": service.package.name,
            },
        )

        logger.info(f"Provisioning completed for service {service_id}, username={username}")

    except (ProvisioningProviderError, WHMClientError) as e:
        logger.error(f"Provider error provisioning service {service_id}: {e}")
        job.last_error = str(e)
        job.status = ProvisioningJob.STATUS_RETRY
        job.save(update_fields=["last_error", "status"])

        try:
            raise self.retry(exc=e)
        except MaxRetriesExceededError:
            job.status = ProvisioningJob.STATUS_FAILED
            job.save(update_fields=["status"])
            service.status = Service.STATUS_FAILED
            service.save(update_fields=["status"])
            logger.error(f"Provisioning permanently failed for service {service_id}")


def create_provisioning_job(service: Service) -> ProvisioningJob:
    """Create a provisioning job and queue it."""
    idempotency_key = f"provision-{service.id}-{uuid.uuid4().hex}"
    job = ProvisioningJob.objects.create(
        service=service,
        idempotency_key=idempotency_key,
        status=ProvisioningJob.STATUS_QUEUED,
    )
    provision_hosting_account.delay(service.id, job.id)
    return job


# ── Email account tasks ───────────────────────────────────────────────────────

@shared_task
def create_email_account_task(service_id: int, email_user: str, domain: str, password: str, quota_mb: int = 500):
    """Create a cPanel email account on behalf of a customer."""
    try:
        service = Service.objects.get(id=service_id)
    except Service.DoesNotExist:
        logger.error(f"create_email_account_task: Service {service_id} not found")
        return

    if not service.cpanel_username:
        logger.error(f"create_email_account_task: Service {service_id} has no cpanel_username")
        return

    try:
        client = WHMClient()
        client.create_email_account(
            cpanel_username=service.cpanel_username,
            email_user=email_user,
            domain=domain,
            password=password,
            quota_mb=quota_mb,
        )
        logger.info(f"Email {email_user}@{domain} created for service {service_id}")
    except WHMClientError as e:
        logger.error(f"Failed to create email {email_user}@{domain} for service {service_id}: {e}")
        raise


@shared_task
def delete_email_account_task(service_id: int, email_user: str, domain: str):
    """Delete a cPanel email account on behalf of a customer."""
    try:
        service = Service.objects.get(id=service_id)
    except Service.DoesNotExist:
        logger.error(f"delete_email_account_task: Service {service_id} not found")
        return

    if not service.cpanel_username:
        return

    try:
        client = WHMClient()
        client.delete_email_account(
            cpanel_username=service.cpanel_username,
            email_user=email_user,
            domain=domain,
        )
        logger.info(f"Email {email_user}@{domain} deleted for service {service_id}")
    except WHMClientError as e:
        logger.error(f"Failed to delete email {email_user}@{domain} for service {service_id}: {e}")
        raise


# ── Database tasks ────────────────────────────────────────────────────────────

@shared_task
def create_database_task(service_id: int, db_name: str):
    """Create a MySQL database for a customer's cPanel account."""
    try:
        service = Service.objects.get(id=service_id)
    except Service.DoesNotExist:
        logger.error(f"create_database_task: Service {service_id} not found")
        return

    if not service.cpanel_username:
        logger.error(f"create_database_task: Service {service_id} has no cpanel_username")
        return

    # cPanel automatically prefixes the db name with the username
    full_name = f"{service.cpanel_username}_{db_name}"
    try:
        client = WHMClient()
        client.create_database(cpanel_username=service.cpanel_username, db_name=full_name)
        logger.info(f"Database {full_name} created for service {service_id}")
    except WHMClientError as e:
        logger.error(f"Failed to create database {full_name} for service {service_id}: {e}")
        raise
