"""Durable, bounded customer requests for registrar verification work."""

from datetime import timedelta
from time import monotonic

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.domains.models import ContactVerification, DomainOrder, VerificationAction, VerificationQueueState
from apps.domains.verification import _identifier, reconcile_registrant_verification

MAX_OPEN_ACTIONS = 500


class VerificationBusy(Exception):
    def __init__(self, retry_after=60):
        self.retry_after = retry_after


class VerificationReviewRequired(Exception):
    pass


def _audit(job, action):
    AuditLog.objects.create(
        user_id=job.actor_id, action=action, model_name="DomainOrder",
        object_id=str(job.domain_order_id), data={"verification_action_id": job.pk, "kind": job.kind, "automatic": job.is_automatic},
    )


def request_verification_action(domain, actor, kind, key, *, automatic=False):
    from apps.accounts.models import User
    authorized = (
        automatic and actor is None and kind == "refresh"
        or not automatic and actor is not None and actor.is_active and domain.user_id == actor.pk
    )
    if kind not in {"refresh", "resend"} or not authorized:
        raise ValidationError("This verification action is not available.")
    with transaction.atomic():
        VerificationQueueState.objects.get_or_create(pk=1)
        VerificationQueueState.objects.select_for_update().get(pk=1)
        previous = VerificationAction.objects.filter(key=key).first()
        if previous:
            if previous.actor_id != getattr(actor, "pk", None) or previous.kind != kind or previous.is_automatic != automatic or previous.domain_order.domain_id != domain.pk:
                raise ValidationError("The submission key belongs to another request.")
            return previous
        order = DomainOrder.objects.select_related("registration_contact").filter(
            domain_id=domain.pk, domain_name=domain.name, user_id=domain.user_id,
        ).first()
        if not order or not _identifier(order.registrar_order_id) or order.registration_contact.user_id != domain.user_id:
            raise ValidationError("A linked registrar order and contact are required.")
        attempt = getattr(order, "registration_attempt", None)
        if attempt and attempt.legacy_review_required:
            raise ValidationError("Historical owner review is required before registrar verification.")
        from apps.domains.registrar_review import registrar_review_required
        if registrar_review_required(domain):
            raise ValidationError("Registrar association review is required before provider verification.")
        from apps.domains.registrar_policy import tld_for_domain
        if tld_for_domain(order.domain_name) != order.tld:
            raise ValidationError("The domain extension does not match the stored order.")
        if not User.objects.filter(pk=domain.user_id, is_active=True).exists():
            raise ValidationError("The domain owner's account is inactive.")
        if not _identifier(order.remote_registrant_id):
            raise ValidationError("The registrar contact must be linked before verification.")
        # RAA email resend is not a universal ccTLD/KYC mechanism.
        if kind == "resend" and order.tld not in {"com", "net", "org"}:
            raise ValidationError("Contact support for this extension's verification process.")
        open_jobs = VerificationAction.objects.filter(status__in=["queued", "running", "unknown"])
        if open_jobs.filter(domain_order=order, kind=kind, status="unknown").exists():
            raise VerificationReviewRequired()
        if open_jobs.filter(domain_order=order, kind=kind).exists() or open_jobs.count() >= MAX_OPEN_ACTIONS:
            raise VerificationBusy()
        cooldown = 900 if automatic else 300 if kind == "resend" else 60
        if VerificationAction.objects.filter(
            domain_order=order, kind=kind, created_at__gt=timezone.now() - timedelta(seconds=cooldown),
        ).exists():
            raise VerificationBusy(cooldown)
        job = VerificationAction.objects.create(
            domain_order=order, actor=actor, kind=kind, key=key,
            subject_digest=ContactVerification.digest_for(order.registration_contact),
            registrar_order_id=order.registrar_order_id, remote_contact_id=order.remote_registrant_id,
            provider_scope=order.registrar_contact_ids.get("_scope", ""),
            is_automatic=automatic,
        )
        _audit(job, "domain.verification.requested")
        transaction.on_commit(lambda: publish_verification_action(job.pk))
        return job


def publish_verification_action(identifier):
    # Eager task settings must not turn a customer HTTP POST into provider work.
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return False
    try:
        run_verification_action.delay(identifier)
    except Exception:
        return False  # Durable queued row remains available for dispatch.
    return True


def _resend_current(job, client, evidence):
    from apps.accounts.models import User
    from apps.domains.models import Domain, DomainContact

    # Keep the local subject/ownership stable through the bounded provider POST.
    # A provider timeout still leaves an uncertain outcome; no automatic retry.
    with transaction.atomic():
        current_job = VerificationAction.objects.select_for_update().get(pk=job.pk)
        order = DomainOrder.objects.select_for_update().get(pk=job.domain_order_id)
        contact = DomainContact.objects.select_for_update().get(pk=order.registration_contact_id)
        domain = Domain.objects.select_for_update().filter(pk=order.domain_id).first()
        actor = User.objects.select_for_update().filter(pk=job.actor_id, is_active=True).first()
        if (
            current_job.status != "running" or current_job.lease_until <= timezone.now()
            or actor is None or domain is None or domain.user_id != job.actor_id
            or order.user_id != job.actor_id or domain.name != order.domain_name
            or order.registrar_contact_ids.get("_scope", "") != job.provider_scope
            or (job.remote_contact_id and order.remote_registrant_id != job.remote_contact_id)
            or order.registrar_order_id != job.registrar_order_id
            or ContactVerification.digest_for(contact) != job.subject_digest
            or not evidence.is_current
        ):
            return None
        return client.resend_registrant_verification(job.registrar_order_id)


@shared_task(name="domains.run_verification_action", ignore_result=True, soft_time_limit=100, time_limit=120)
def run_verification_action(identifier):
    from apps.core.runtime_settings import get_runtime_setting
    from apps.domains.resellerclub_client import ResellerClubClient
    from apps.core.abuse import consume_limit, AbuseLimitExceeded, AbuseStoreUnavailable

    with transaction.atomic():
        job = VerificationAction.objects.select_for_update(of=("self",)).select_related("domain_order__registration_contact", "domain_order__domain", "domain_order__user", "actor").get(pk=identifier)
        if job.status != "queued" or job.available_at > timezone.now():
            return
        order = job.domain_order
        authorized = (
            job.is_automatic and job.kind == "refresh" and order.user.is_active
            or not job.is_automatic and job.actor is not None and job.actor.is_active and order.user_id == job.actor_id
        )
        if (
            not authorized
            or not order.domain_id or order.domain.user_id != order.user_id or order.domain.name != order.domain_name
            or order.registrar_contact_ids.get("_scope", "") != job.provider_scope
            or (job.remote_contact_id and order.remote_registrant_id != job.remote_contact_id)
            or order.registrar_order_id != job.registrar_order_id
            or ContactVerification.digest_for(order.registration_contact) != job.subject_digest
        ):
            job.status = "cancelled"
            job.completed_at = timezone.now()
            job.save(update_fields=["status", "completed_at", "updated_at"])
            _audit(job, "domain.verification.cancelled")
            return
        try:
            consume_limit("registrar-verification-worker", "global", 30, 60)
        except (AbuseLimitExceeded, AbuseStoreUnavailable) as exc:
            job.available_at = timezone.now() + timedelta(seconds=getattr(exc, "retry_after", 60))
            job.save(update_fields=["available_at", "updated_at"])
            return
        job.status = "running"
        job.lease_until = timezone.now() + timedelta(minutes=3)
        job.save(update_fields=["status", "lease_until", "updated_at"])

    mutation_started = False
    status = "failed"
    try:
        client = ResellerClubClient()
        evidence = reconcile_registrant_verification(
            order.pk, get_runtime_setting("RESELLERCLUB_CUSTOMER_ID", ""), client=client,
        )
        if job.kind == "refresh":
            status = "succeeded"
        elif evidence.status in {"pending", "suspended"} and evidence.is_current:
            mutation_started = True
            accepted = _resend_current(job, client, evidence)
            status = (
                "cancelled" if accepted is None else "unknown" if type(accepted) is not bool
                else "succeeded" if accepted else "failed"
            )
        else:
            # A verified/unknown response never sends another email.
            status = "cancelled"
    except Exception:
        # No raw provider exceptions or contact data enter the job/audit record.
        status = "unknown" if mutation_started else "failed"
    with transaction.atomic():
        job = VerificationAction.objects.select_for_update().get(pk=identifier)
        if job.status == "running":
            job.status = status
            job.completed_at = timezone.now()
            job.lease_until = None
            job.save(update_fields=["status", "completed_at", "lease_until", "updated_at"])
            _audit(job, f"domain.verification.{status}")


@shared_task(name="domains.dispatch_verification_actions", ignore_result=True, soft_time_limit=20, time_limit=30)
def dispatch_verification_actions():
    now = timezone.now()
    # An expired resend lease is uncertain even if a worker died before send.
    # Conservative review prevents automatic duplicate email.
    stale = list(VerificationAction.objects.filter(status="running", lease_until__lt=now).values_list("pk", flat=True)[:100])
    for identifier in stale:
        with transaction.atomic():
            job = VerificationAction.objects.select_for_update().get(pk=identifier)
            if job.status != "running" or job.lease_until >= now:
                continue
            job.status = "unknown" if job.kind == "resend" else "failed"
            job.completed_at = now
            job.lease_until = None
            job.save(update_fields=["status", "completed_at", "lease_until", "updated_at"])
            _audit(job, f"domain.verification.{job.status}")
    for identifier in VerificationAction.objects.filter(status="queued", available_at__lte=now).order_by("is_automatic", "created_at", "pk").values_list("pk", flat=True)[:100]:
        publish_verification_action(identifier)


@shared_task(name="domains.schedule_verification_checks", ignore_result=True, soft_time_limit=20, time_limit=30)
def schedule_verification_checks():
    """Advance a durable cursor through linked RAA orders, at most 100 per run.

    This only schedules read-only refreshes. No provider call or resend occurs
    during the scan. Fresh evidence and existing/cooling-down jobs are skipped.
    """
    import uuid
    from django.db.models import F

    admitted = 0
    deadline = monotonic() + 8
    with transaction.atomic():
        VerificationQueueState.objects.get_or_create(pk=1)
        state = VerificationQueueState.objects.select_for_update().get(pk=1)
        if VerificationAction.objects.filter(status__in=["queued", "running", "unknown"]).count() >= MAX_OPEN_ACTIONS:
            return 0
        orders = list(DomainOrder.objects.filter(
            pk__gt=state.scan_after, status="completed", user__is_active=True,
            domain__status__in=["active", "expired", "suspended"],
            domain__user_id=F("user_id"), domain__name=F("domain_name"),
            tld__in=["com", "net", "org"],
        ).exclude(registrar_order_id="").select_related("domain", "registration_contact").order_by("pk")[:100])
        if not orders:
            state.scan_after = 0
        for order in orders:
            if monotonic() >= deadline:
                break
            state.scan_after = order.pk
            latest = order.verification_evidence.filter(
                check_kind="registrar_email", source="resellerclub",
            ).first()
            if latest and latest.is_current:
                continue
            try:
                request_verification_action(order.domain, None, "refresh", uuid.uuid4(), automatic=True)
            except (ValidationError, VerificationBusy, VerificationReviewRequired):
                continue
            admitted += 1
        state.save(update_fields=["scan_after"])
    return admitted
