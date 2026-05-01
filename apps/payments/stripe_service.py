"""Stripe payment service."""
import logging
import stripe
from django.conf import settings
from django.urls import reverse

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


class StripeService:
    """Handles Stripe payment operations."""

    @staticmethod
    def create_checkout_session(invoice, request) -> str:
        """Create a Stripe Checkout Session for an invoice and return the session URL."""
        line_items = []
        for item in invoice.line_items.all():
            line_items.append({
                "price_data": {
                    "currency": "gbp",
                    "unit_amount": int(item.unit_price * 100),
                    "product_data": {
                        "name": item.description,
                    },
                },
                "quantity": int(item.quantity),
            })

        if invoice.vat_amount > 0:
            line_items.append({
                "price_data": {
                    "currency": "gbp",
                    "unit_amount": int(invoice.vat_amount * 100),
                    "product_data": {
                        "name": "VAT",
                    },
                },
                "quantity": 1,
            })

        success_url = request.build_absolute_uri(
            reverse("payments:stripe_success") + f"?session_id={{CHECKOUT_SESSION_ID}}&invoice_id={invoice.id}"
        )
        cancel_url = request.build_absolute_uri(
            reverse("invoices:detail", kwargs={"pk": invoice.id})
        )

        try:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=line_items,
                mode="payment",
                success_url=success_url,
                cancel_url=cancel_url,
                customer_email=invoice.user.email,
                metadata={
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.number,
                },
            )
            return session.url
        except stripe.error.StripeError as e:
            logger.error(f"Stripe checkout session creation failed: {e}")
            raise

    @staticmethod
    def handle_webhook(payload: bytes, sig_header: str):
        """Verify and process a Stripe webhook event."""
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"Invalid Stripe webhook signature: {e}")
            raise ValueError("Invalid signature")

        return event

    @staticmethod
    def create_refund(payment_intent_id: str, amount_pence: int = None) -> dict:
        """Create a refund for a payment."""
        params = {"payment_intent": payment_intent_id}
        if amount_pence:
            params["amount"] = amount_pence
        return stripe.Refund.create(**params)
