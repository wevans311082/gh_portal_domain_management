from celery import shared_task
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def provision_service(self, job_id):
    from .models import ProvisioningJob
    try:
        job = ProvisioningJob.objects.get(id=job_id)
        job.status = ProvisioningJob.STATUS_IN_PROGRESS
        job.celery_task_id = self.request.id
        job.attempt_count += 1
        job.save(update_fields=["status", "celery_task_id", "attempt_count"])
        logger.info(f"Provisioning job {job_id} started")
    except ProvisioningJob.DoesNotExist:
        logger.error(f"ProvisioningJob {job_id} not found")
