"""Stripe payment service."""
import logging
import stripe
from django.urls import reverse

from apps.core.runtime_settings import get_runtime_setting

logger = logging.getLogger(__name__)


class StripeService:
    """Handles Stripe payment operations."""

    @staticmethod
    def create_checkout_session(invoice, request) -> str:
        """Create a Stripe Checkout Session for an invoice and return the session URL."""
        stripe.api_key = get_runtime_setting("STRIPE_SECRET_KEY", "")
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
        stripe.api_key = get_runtime_setting("STRIPE_SECRET_KEY", "")
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, get_runtime_setting("STRIPE_WEBHOOK_SECRET", "")
            )
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"Invalid Stripe webhook signature: {e}")
            raise ValueError("Invalid signature")

        return event

    @staticmethod
    def create_refund(payment_intent_id: str, amount_pence: int = None) -> dict:
        """Create a refund for a payment."""
        stripe.api_key = get_runtime_setting("STRIPE_SECRET_KEY", "")
        params = {"payment_intent": payment_intent_id}
        if amount_pence:
            params["amount"] = amount_pence
        return stripe.Refund.create(**params)

    @staticmethod
    def get_or_create_customer(user) -> str:
        """Return existing Stripe Customer ID or create one for the user."""
        from apps.payments.models import StripeCustomer
        stripe.api_key = get_runtime_setting("STRIPE_SECRET_KEY", "")
        try:
            sc = StripeCustomer.objects.get(user=user)
            return sc.stripe_customer_id
        except StripeCustomer.DoesNotExist:
            customer = stripe.Customer.create(email=user.email, name=user.get_full_name() or user.email)
            StripeCustomer.objects.create(user=user, stripe_customer_id=customer["id"])
            return customer["id"]

    @staticmethod
    def create_setup_intent(user) -> dict:
        """Create a Stripe SetupIntent to save a card for future use."""
        stripe.api_key = get_runtime_setting("STRIPE_SECRET_KEY", "")
        customer_id = StripeService.get_or_create_customer(user)
        intent = stripe.SetupIntent.create(
            customer=customer_id,
            payment_method_types=["card"],
            usage="off_session",
        )
        return {"client_secret": intent["client_secret"], "setup_intent_id": intent["id"]}

    @staticmethod
    def attach_payment_method(user, stripe_pm_id: str, set_as_default: bool = True):
        """Attach a confirmed payment method to the user and save it to the DB."""
        from apps.payments.models import SavedPaymentMethod
        stripe.api_key = get_runtime_setting("STRIPE_SECRET_KEY", "")
        customer_id = StripeService.get_or_create_customer(user)

        pm = stripe.PaymentMethod.retrieve(stripe_pm_id)
        stripe.PaymentMethod.attach(stripe_pm_id, customer=customer_id)

        card = pm.get("card", {})
        saved, _ = SavedPaymentMethod.objects.update_or_create(
            stripe_pm_id=stripe_pm_id,
            defaults={
                "user": user,
                "last4": card.get("last4", ""),
                "brand": card.get("brand", "card"),
                "exp_month": card.get("exp_month", 0),
                "exp_year": card.get("exp_year", 0),
                "is_default": set_as_default,
            },
        )
        return saved

    @staticmethod
    def charge_saved_payment_method(user, amount_pence: int, description: str, invoice=None) -> dict:
        """Charge the user's default saved payment method via PaymentIntent."""
        from apps.payments.models import SavedPaymentMethod, Payment
        stripe.api_key = get_runtime_setting("STRIPE_SECRET_KEY", "")

        default_pm = SavedPaymentMethod.objects.filter(user=user, is_default=True).first()
        if not default_pm:
            raise ValueError("No default payment method on file.")

        customer_id = StripeService.get_or_create_customer(user)
        intent = stripe.PaymentIntent.create(
            amount=amount_pence,
            currency="gbp",
            customer=customer_id,
            payment_method=default_pm.stripe_pm_id,
            off_session=True,
            confirm=True,
            description=description,
            metadata={"invoice_id": str(invoice.id)} if invoice else {},
        )

        payment = Payment.objects.create(
            user=user,
            invoice=invoice,
            provider=Payment.PROVIDER_STRIPE,
            status=Payment.STATUS_COMPLETED if intent["status"] == "succeeded" else Payment.STATUS_PENDING,
            amount=amount_pence / 100,
            external_id=intent["id"],
            provider_data={"payment_intent_id": intent["id"]},
        )
        return {"payment": payment, "intent": intent}
