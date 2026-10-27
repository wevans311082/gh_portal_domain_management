"""Transactional public receipts and delivery jobs; no provider calls here."""
import json
import hashlib
import logging
import uuid

from django.conf import settings
from django.db import OperationalError, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import PublicSubmission, PublicDelivery, PublicDeliveryGate

logger = logging.getLogger(__name__)
MAX_PENDING_DELIVERIES = 1000


class SubmissionUnavailable(ValueError):
    pass


class SubmissionConflict(ValueError):
    pass


def _digest(value):
    return salted_hmac("public-submission-v1", value, algorithm="sha256").hexdigest()


def submission_owner(request):
    marker = request.session.get("_public_submission_owner")
    if not marker:
        marker = uuid.uuid4().hex
        request.session["_public_submission_owner"] = marker
    return _digest(f"{getattr(request.user, 'pk', '') or ''}:{marker}")


def new_submission_key(request):
    submission_owner(request)  # Persist the owner marker on the GET before POST.
    return str(uuid.uuid4())


def publish_delivery(identifier):
    from .tasks import deliver_public_submission
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        logger.warning("Public delivery requires asynchronous execution job=%s", identifier)
        return
    try:
        deliver_public_submission.apply_async(args=[str(identifier)], expires=300)
    except Exception:
        # The durable queued row is recovered by the bounded dispatcher.
        logger.error("Public delivery publish deferred job=%s", identifier)


def lock_delivery_gate():
    """Serialize admission decisions; caller holds an atomic transaction."""
    PublicDeliveryGate.objects.get_or_create(pk=1)
    PublicDeliveryGate.objects.select_for_update().get(pk=1)


def reserve_delivery_capacity():
    """Caller holds an atomic transaction until its delivery row is created."""
    lock_delivery_gate()
    if PublicDelivery.objects.filter(state__in=["queued", "preparing", "delivering", "failed", "unknown"]).count() >= MAX_PENDING_DELIVERIES:
        raise SubmissionUnavailable("This service is temporarily busy. Please try again later.")


def _accept_submission(request, kind, payload, create_result, key=None, requires_delivery=True):
    """Create once under a unique key; compare payload/owner before replay."""
    owner = submission_owner(request)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload_digest = _digest(encoded)
    if key is not None:
        try:
            explicit_key = str(uuid.UUID(str(key)))
        except (ValueError, TypeError, AttributeError):
            raise SubmissionConflict("The submission key is invalid. Reload the form and try again.") from None
        # Opaque UUID keys must remain globally unique across Django signing-key
        # rotation. Their high-entropy digest contains no email/body identity.
        receipt_key = hashlib.sha256(f"{kind}:explicit:{explicit_key}".encode()).hexdigest()
    else:
        # Legacy clients without a key still deduplicate identical session data.
        # A fresh form supplies a fresh key for an intentional new submission.
        receipt_key = _digest(f"{kind}:fallback:{owner}:{payload_digest}")
    with transaction.atomic():
        receipt, created = PublicSubmission.objects.get_or_create(key=receipt_key, defaults={
            "kind": kind, "owner_digest": owner, "payload_digest": payload_digest,
        })
        receipt = PublicSubmission.objects.select_for_update().get(pk=receipt.pk)
        if not created:
            if receipt.owner_digest != owner or receipt.payload_digest != payload_digest:
                raise SubmissionConflict("This submission key has already been used. Reload the form for a new request.")
            return receipt.result, False
        if requires_delivery:
            reserve_delivery_capacity()
        result, delivery_data = create_result()
        if delivery_data is not None and not requires_delivery:
            raise ValueError("Delivery admission was not reserved")
        receipt.result = result
        receipt.save(update_fields=["result", "updated_at"])
        if delivery_data is not None:
            job = PublicDelivery.objects.create(submission=receipt, data=delivery_data, available_at=timezone.now())
            transaction.on_commit(lambda identifier=job.pk: publish_delivery(identifier))
        return result, True


def accept_submission(request, kind, payload, create_result, key=None, requires_delivery=True):
    try:
        return _accept_submission(request, kind, payload, create_result, key=key, requires_delivery=requires_delivery)
    except OperationalError:
        # In particular, SQLite can reject concurrent writers before a unique
        # receipt can be observed. The transaction has rolled back; a retry is
        # safe and must not expose database details or create partial records.
        raise SubmissionUnavailable("This service is temporarily busy. Please retry the same submission shortly.") from None


def accept_contact(request, cleaned, configuration):
    from .http import get_client_ip
    from .models import ContactFormSettings, ContactSubmission
    values = {key: cleaned[key] for key in ("name", "email", "phone", "subject", "message")}
    email_destination = configuration.destination in {ContactFormSettings.DESTINATION_EMAIL, ContactFormSettings.DESTINATION_BOTH}
    if email_destination and not configuration.destination_email:
        raise ValueError("The contact service is temporarily unavailable. Please try again later.")
    def create_result():
        contact = None
        if configuration.destination in {ContactFormSettings.DESTINATION_DB, ContactFormSettings.DESTINATION_BOTH}:
            contact = ContactSubmission.objects.create(**values, ip_address=get_client_ip(request) or None,
                                                       user_agent=request.META.get("HTTP_USER_AGENT", "")[:512])
        delivery = {"recipient": configuration.destination_email, "contact": values} if email_destination else None
        return {"contact_id": contact.pk if contact else None}, delivery
    return accept_submission(request, "contact", values, create_result, key=cleaned.get("submission_key"), requires_delivery=email_destination)
