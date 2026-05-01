"""Celery tasks for hosting provisioning."""
import logging
import uuid
from django.utils import timezone
from django.db import transaction
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

from apps.services.models import Service
from apps.provisioning.models import ProvisioningJob
from apps.provisioning.whm_client import WHMClient, WHMClientError, generate_cpanel_username, generate_secure_password

logger = logging.getLogger(__name__)


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
    job.celery_task_id = self.request.id
    job.save(update_fields=["status", "attempt_count", "celery_task_id"])

    try:
        client = WHMClient()
        username = generate_cpanel_username(service.domain_name or service.user.email.split("@")[0])
        password = generate_secure_password()

        result = client.create_account(
            domain=service.domain_name,
            username=username,
            password=password,
            package=service.package.whm_package_name,
            email=service.user.email,
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
            job.result_data = {"username": username, "result": str(result)}
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

    except WHMClientError as e:
        logger.error(f"WHM error provisioning service {service_id}: {e}")
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
