"""Payment views - Stripe, GoCardless, PayPal."""
import hashlib
import hmac
import logging
import json
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.utils import timezone
from django.db import transaction

from apps.core.runtime_settings import get_runtime_setting

from apps.billing.models import Invoice
from apps.domains.models import DomainOrder, DomainRenewal
from apps.domains.tasks import register_domain_order, execute_domain_renewal
from apps.payments.models import Payment, WebhookEvent
from apps.payments.stripe_service import StripeService

logger = logging.getLogger(__name__)


@login_required
def stripe_checkout(request, invoice_id):
    """Redirect to Stripe Checkout for an invoice."""
    invoice = get_object_or_404(Invoice, pk=invoice_id, user=request.user)
    if invoice.status == Invoice.STATUS_PAID:
        messages.info(request, "This invoice has already been paid.")
        return redirect("invoices:detail", pk=invoice_id)

    try:
        checkout_url = StripeService.create_checkout_session(invoice, request)
        return redirect(checkout_url)
    except Exception as e:
        logger.error(f"Stripe checkout failed for invoice {invoice_id}: {e}")
        messages.error(request, "Unable to process payment. Please try again.")
        return redirect("invoices:detail", pk=invoice_id)


@login_required
def stripe_success(request):
    """Handle successful Stripe payment redirect."""
    invoice_id = request.GET.get("invoice_id")
    if invoice_id:
        invoice = get_object_or_404(Invoice, pk=invoice_id, user=request.user)
        messages.success(request, f"Payment received for invoice #{invoice.number}. Thank you!")
        return redirect("invoices:detail", pk=invoice_id)
    return redirect("portal:dashboard")


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Handle incoming Stripe webhook events."""
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        event = StripeService.handle_webhook(payload, sig_header)
    except ValueError as e:
        logger.warning(f"Invalid Stripe webhook: {e}")
        return HttpResponse(status=400)

    event_id = event["id"]
    event_type = event["type"]

    # Atomic idempotency: use get_or_create to avoid TOCTOU race between workers
    with transaction.atomic():
        webhook_event, created = WebhookEvent.objects.get_or_create(
            event_id=event_id,
            defaults={
                "provider": Payment.PROVIDER_STRIPE,
                "event_type": event_type,
                "payload": event,
            },
        )
        if not created:
            logger.info(f"Stripe webhook {event_id} already processed, skipping.")
            return HttpResponse(status=200)

    try:
        _process_stripe_event(event, webhook_event)
        webhook_event.processed = True
        webhook_event.processed_at = timezone.now()
        webhook_event.save(update_fields=["processed", "processed_at"])
    except Exception as e:
        logger.error(f"Error processing Stripe webhook {event_id}: {e}")
        webhook_event.processing_error = str(e)
        webhook_event.save(update_fields=["processing_error"])
        return HttpResponse(status=500)

    return HttpResponse(status=200)


def _process_stripe_event(event: dict, webhook_event: WebhookEvent):
    """Route Stripe event to the appropriate handler."""
    event_type = event["type"]
    data = event["data"]["object"]

    handlers = {
        "checkout.session.completed": _handle_checkout_completed,
        "invoice.paid": _handle_invoice_paid,
        "invoice.payment_failed": _handle_invoice_payment_failed,
        "payment_intent.succeeded": _handle_payment_intent_succeeded,
        "payment_intent.payment_failed": _handle_payment_intent_failed,
    }

    handler = handlers.get(event_type)
    if handler:
        handler(data, webhook_event)
    else:
        logger.debug(f"Unhandled Stripe event type: {event_type}")


def _handle_checkout_completed(session: dict, webhook_event: WebhookEvent):
    """Handle checkout.session.completed event."""
    invoice_id = session.get("metadata", {}).get("invoice_id")
    if not invoice_id:
        return

    try:
        invoice = Invoice.objects.get(id=invoice_id)
        amount = session.get("amount_total", 0) / 100

        Payment.objects.create(
            user=invoice.user,
            invoice=invoice,
            provider=Payment.PROVIDER_STRIPE,
            status=Payment.STATUS_COMPLETED,
            amount=amount,
            currency=session.get("currency", "gbp").upper(),
            external_id=session.get("payment_intent", ""),
            provider_data=session,
        )

        from apps.billing.services import mark_invoice_paid
        mark_invoice_paid(invoice)

        logger.info(f"Invoice {invoice_id} marked as paid via Stripe checkout.")
    except Invoice.DoesNotExist:
        logger.error(f"Invoice {invoice_id} not found for Stripe checkout.session.completed")


def _queue_paid_domain_orders(invoice: Invoice):
    pending_orders = DomainOrder.objects.filter(
        invoice=invoice,
        status__in=[DomainOrder.STATUS_PENDING_PAYMENT, DomainOrder.STATUS_DRAFT, DomainOrder.STATUS_PAID],
    )
    for order in pending_orders:
        order.status = DomainOrder.STATUS_PAID
        order.save(update_fields=["status", "updated_at"])
        register_domain_order.delay(order.id)

    pending_renewals = DomainRenewal.objects.filter(
        invoice=invoice,
        status=DomainRenewal.STATUS_PENDING_PAYMENT,
    )
    for renewal in pending_renewals:
        renewal.status = DomainRenewal.STATUS_PAID
        renewal.save(update_fields=["status"])
        execute_domain_renewal.delay(renewal.id)


def _handle_invoice_paid(stripe_invoice: dict, webhook_event: WebhookEvent):
    """Handle invoice.paid event (for subscriptions)."""
    stripe_invoice_id = stripe_invoice.get("id")
    try:
        invoice = Invoice.objects.get(stripe_invoice_id=stripe_invoice_id)
        from apps.billing.services import mark_invoice_paid
        mark_invoice_paid(invoice)
        logger.info(f"Stripe invoice {stripe_invoice_id} marked paid.")
    except Invoice.DoesNotExist:
        logger.debug(f"No local invoice found for Stripe invoice {stripe_invoice_id}")


def _handle_invoice_payment_failed(stripe_invoice: dict, webhook_event: WebhookEvent):
    """Handle invoice.payment_failed event."""
    logger.warning(f"Stripe invoice payment failed: {stripe_invoice.get('id')}")


def _handle_payment_intent_succeeded(pi: dict, webhook_event: WebhookEvent):
    """Handle payment_intent.succeeded."""
    logger.info(f"Payment intent succeeded: {pi.get('id')}")


def _handle_payment_intent_failed(pi: dict, webhook_event: WebhookEvent):
    """Handle payment_intent.payment_failed."""
    logger.warning(f"Payment intent failed: {pi.get('id')}")


@csrf_exempt
@require_POST
def gocardless_webhook(request):
    """Handle GoCardless webhooks with HMAC signature verification."""
    payload = request.body

    # Verify the webhook signature using the GoCardless webhook secret
    webhook_secret = get_runtime_setting("GOCARDLESS_WEBHOOK_SECRET", "")
    if not webhook_secret:
        logger.error("GOCARDLESS_WEBHOOK_SECRET is not configured; rejecting webhook.")
        return HttpResponse(status=400)

    provided_signature = request.headers.get("Webhook-Signature", "")
    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(provided_signature, expected_signature):
        logger.warning(
            "GoCardless webhook signature mismatch — possible spoofed request."
        )
        return HttpResponse(status=498)  # 498 = Token Required / Invalid

    try:
        events = json.loads(payload).get("events", [])
    except (json.JSONDecodeError, AttributeError):
        return HttpResponse(status=400)

    for event in events:
        event_id = event.get("id", "")
        event_type = f"{event.get('resource_type', '')}.{event.get('action', '')}"

        with transaction.atomic():
            _, created = WebhookEvent.objects.get_or_create(
                event_id=event_id,
                defaults={
                    "provider": Payment.PROVIDER_GOCARDLESS,
                    "event_type": event_type,
                    "payload": event,
                    "processed": True,
                    "processed_at": timezone.now(),
                },
            )
            if not created:
                logger.info(f"GoCardless webhook {event_id} already processed, skipping.")

    return HttpResponse(status=200)
