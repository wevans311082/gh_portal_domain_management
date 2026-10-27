"""Durable public delivery with bounded preparation and uncertain-send states."""
from datetime import timedelta
import logging
import uuid

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from .models import PublicDelivery

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
LEASE_SECONDS = 120


def prepare_public_message(job):
    if job.submission.kind == "account":
        from apps.accounts.mail_delivery import prepare_account_mail
        return prepare_account_mail(job)
    if job.submission.kind == "invoice":
        from apps.billing.models import Invoice
        from apps.billing.services import email_document
        invoice = Invoice.objects.select_related("user").get(pk=job.data["invoice_id"])
        if not invoice.user.is_active:
            return None
        return email_document(invoice, kind="invoice_issued", prepare_only=True)
    if job.submission.kind == "quote":
        from apps.billing.models import Quote
        from apps.billing.services import email_document
        quote = Quote.objects.select_related("user").get(pk=job.data["quote_id"])
        if quote.user and not quote.user.is_active:
            return None
        return email_document(quote, kind="quote_sent", recipient_email=job.data["recipient"], prepare_only=True)
    if job.submission.kind != "contact":
        raise ValueError("Unsupported public delivery type")
    values = job.data["contact"]
    html = render_to_string("emails/contact_submission.html", values)
    # Header text is single-line and bounded independently of message body.
    subject = " ".join(f"[Contact] {values['subject'] or 'New enquiry'} from {values['name']}".splitlines())[:200]
    message = EmailMultiAlternatives(subject=subject, body=strip_tags(html), from_email=settings.DEFAULT_FROM_EMAIL,
                                    to=[job.data["recipient"]], headers={"X-CyberAsk-Mailbox-Purpose": "support"})
    message.attach_alternative(html, "text/html")
    return message


@shared_task(name="core.deliver_public_submission", acks_late=True, soft_time_limit=35, time_limit=45, ignore_result=True)
def deliver_public_submission(identifier):
    now = timezone.now()
    token = uuid.uuid4()
    with transaction.atomic():
        job = PublicDelivery.objects.select_for_update().select_related("submission").filter(pk=identifier).first()
        if not job or job.state != "queued" or job.available_at > now or job.attempts >= MAX_ATTEMPTS:
            return "skipped"
        job.state, job.lease_token, job.claimed_at = "preparing", token, now
        job.attempts += 1
        job.total_attempts += 1
        job.error_code = ""
        job.save(update_fields=["state", "lease_token", "claimed_at", "attempts", "total_attempts", "error_code", "updated_at"])
    claim = PublicDelivery.objects.filter(pk=identifier, state="preparing", lease_token=token)
    try:
        message = prepare_public_message(job)
        if message is None:
            claim.update(state="cancelled", data={}, completed_at=timezone.now(), lease_token=None)
            return "cancelled"
        # No unbounded attachments or repeated provider sends through this path.
        if sum(len(attachment.content) for attachment in message.attachments) > 10 * 1024 * 1024:
            raise ValueError("Public document attachment limit exceeded")
        message.extra_headers["Message-ID"] = f"<public-{job.pk}@portal.invalid>"
        message.extra_headers["X-CyberAsk-No-Fallback"] = "1"
    except Exception:
        retry = job.attempts < MAX_ATTEMPTS
        claim.update(state="queued" if retry else "failed", lease_token=None, error_code="preparation_failed",
                     available_at=timezone.now() + timedelta(seconds=60 * job.attempts))
        logger.error("Public message preparation failed job=%s attempt=%s", identifier, job.attempts)
        return "retry" if retry else "failed"
    # Persist the sending boundary before entering an external delivery call.
    if not claim.update(state="delivering", claimed_at=timezone.now()):
        return "lease_lost"
    sending = PublicDelivery.objects.filter(pk=identifier, state="delivering", lease_token=token)
    try:
        delivered = message.send(fail_silently=False)
        if delivered != 1:
            raise RuntimeError("No delivery acknowledgment")
    except Exception:
        sending.update(state="unknown", error_code="delivery_uncertain", lease_token=None, completed_at=timezone.now())
        logger.error("Public delivery requires review job=%s", identifier)
        return "unknown"
    sending.update(state="sent", data={}, error_code="", lease_token=None, completed_at=timezone.now())
    try:
        from apps.audit.models import EmailLog
        EmailLog.objects.create(recipient=message.to[0], subject=message.subject[:255],
                                template=getattr(message, "_account_template", {"quote": "quote_sent", "invoice": "invoice_issued", "contact": "contact_submission", "account": "account_mail"}[job.submission.kind])[:100], status="sent")
    except Exception:
        logger.error("Public sent-mail audit unavailable job=%s", identifier)
    return "sent"


@shared_task(name="core.dispatch_public_deliveries", ignore_result=True, soft_time_limit=20, time_limit=30)
def dispatch_public_deliveries():
    now = timezone.now()
    stale = now - timedelta(seconds=LEASE_SECONDS)
    # A worker may have sent after entering delivering. Never resend it blindly.
    PublicDelivery.objects.filter(state="delivering", claimed_at__lt=stale).update(
        state="unknown", error_code="delivery_interrupted", lease_token=None, completed_at=now)
    PublicDelivery.objects.filter(state="preparing", claimed_at__lt=stale, attempts__gte=MAX_ATTEMPTS).update(
        state="failed", error_code="preparation_interrupted", lease_token=None)
    PublicDelivery.objects.filter(state="preparing", claimed_at__lt=stale, attempts__lt=MAX_ATTEMPTS).update(
        state="queued", error_code="preparation_interrupted", lease_token=None, available_at=now)
    from .public_submissions import publish_delivery
    identifiers = list(PublicDelivery.objects.filter(state="queued", available_at__lte=now, attempts__lt=MAX_ATTEMPTS)
                       .order_by("available_at").values_list("pk", flat=True)[:100])
    for identifier in identifiers:
        publish_delivery(identifier)
    return len(identifiers)
