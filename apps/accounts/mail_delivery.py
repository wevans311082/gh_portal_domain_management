"""Encrypted, expiring account-mail payloads in the shared durable outbox."""
import json
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db import OperationalError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.admin_tools.secrets import encrypt_value, decrypt_value, IntegrationSecretError
from apps.core.models import PublicSubmission, PublicDelivery
from apps.core.public_submissions import lock_delivery_gate, reserve_delivery_capacity, publish_delivery, SubmissionUnavailable

MAX_MESSAGE_BYTES = 256 * 1024
LIFETIME_SECONDS = 900


def queue_account_mail(message, template, user=None):
    """Deduplicate outstanding mail and recent copies; never send in HTTP."""
    if message.attachments or len(message.to) != 1 or message.cc or message.bcc:
        raise SubmissionUnavailable("Account mail could not be queued.")
    values = {"subject": message.subject, "body": message.body, "from_email": message.from_email,
              "to": message.to, "alternatives": list(message.alternatives), "subtype": message.content_subtype,
              "template": template, "user_id": getattr(user, "pk", None),
              "user_state": user.get_session_auth_hash() if user and user.pk else None}
    encoded = json.dumps(values, sort_keys=True, ensure_ascii=True)
    if len(encoded.encode()) > MAX_MESSAGE_BYTES or len(message.subject) > 255:
        raise SubmissionUnavailable("Account mail could not be queued.")
    now = timezone.now()
    digest = salted_hmac("account-mail-payload-v1", encoded, algorithm="sha256").hexdigest()
    key = salted_hmac("account-mail-receipt-v1", f"{int(now.timestamp()) // 60}:{digest}", algorithm="sha256").hexdigest()
    try:
        with transaction.atomic():
            receipt, created = PublicSubmission.objects.get_or_create(key=key, defaults={
                "kind": "account", "owner_digest": digest, "payload_digest": digest,
            })
            if not created:
                return receipt.delivery.pk
            # A minute boundary must not create duplicate outstanding work.
            # Serialize against other admitted messages before checking it.
            lock_delivery_gate()
            existing = PublicDelivery.objects.filter(submission__kind="account", submission__payload_digest=digest).filter(
                Q(state__in=["queued", "preparing", "delivering", "failed", "unknown"]) |
                Q(created_at__gte=now - timedelta(seconds=60))).order_by("-created_at").first()
            if existing:
                receipt.delete()
                return existing.pk
            reserve_delivery_capacity()
            job_id = uuid.uuid4()
            values["expires_at"] = (now + timedelta(seconds=LIFETIME_SECONDS)).isoformat()
            packet = encrypt_value(f"account-mail:{job_id}", json.dumps(values, ensure_ascii=True))
            job = PublicDelivery.objects.create(id=job_id, submission=receipt, data={"envelope": packet}, available_at=now)
            transaction.on_commit(lambda identifier=job.pk: publish_delivery(identifier))
            return job.pk
    except (OperationalError, IntegrationSecretError):
        raise SubmissionUnavailable("Account mail is temporarily unavailable. Please try again later.") from None


def prepare_account_mail(job):
    from datetime import datetime
    packet = job.data["envelope"]
    if not isinstance(packet, str) or len(packet) > 2 * MAX_MESSAGE_BYTES:
        raise ValueError("Invalid account-mail envelope")
    values = json.loads(decrypt_value(f"account-mail:{job.pk}", packet))
    if timezone.now() >= datetime.fromisoformat(values["expires_at"]):
        return None
    if values["user_id"] is not None:
        user = get_user_model().objects.filter(pk=values["user_id"], is_active=True).first()
        if user is None or user.get_session_auth_hash() != values["user_state"]:
            return None
        recipient = values["to"][0]
        if user.email.casefold() != recipient.casefold():
            from allauth.account.models import EmailAddress
            if not EmailAddress.objects.filter(user=user, email__iexact=recipient).exists():
                return None
    message = EmailMultiAlternatives(values["subject"], values["body"], values["from_email"], values["to"],
                                     headers={"X-CyberAsk-Mailbox-Purpose": "notifications"})
    message._account_template = values["template"]
    message.content_subtype = values["subtype"]
    for body, mimetype in values["alternatives"]:
        message.attach_alternative(body, mimetype)
    return message
