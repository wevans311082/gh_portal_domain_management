"""Wire-level SDK checks for the dependency upgrade, without live providers."""
from urllib.parse import parse_qs

import pytest
import responses

from apps.admin_tools.models import IntegrationSetting
from apps.payments.stripe_service import StripeService

pytestmark = pytest.mark.django_db


@responses.activate
def test_upgraded_stripe_sdk_preserves_refund_wire_version_and_parameters():
    IntegrationSetting.set_value("STRIPE_SECRET_KEY", "sk_test_fixture_only")
    responses.add(responses.POST, "https://api.stripe.com/v1/refunds", json={"id": "re_fixture", "object": "refund"})
    refund = StripeService.create_refund("pi_fixture", 1200)
    assert refund["id"] == "re_fixture"
    request = responses.calls[0].request
    assert request.headers["Stripe-Version"] == "2023-10-16"
    assert request.headers["Authorization"] == "Bearer sk_test_fixture_only"
    assert parse_qs(request.body) == {"payment_intent": ["pi_fixture"], "amount": ["1200"]}


@responses.activate
def test_upgraded_sdk_customer_and_setup_intent_round_trip(django_user_model):
    user = django_user_model.objects.create_user(email="sdk-customer@example.test")
    IntegrationSetting.set_value("STRIPE_SECRET_KEY", "sk_test_fixture_only")
    responses.add(responses.POST, "https://api.stripe.com/v1/customers", json={"id": "cus_fixture", "object": "customer"})
    responses.add(responses.POST, "https://api.stripe.com/v1/setup_intents", json={
        "id": "seti_fixture", "object": "setup_intent", "client_secret": "fixture_client_secret",
    })
    result = StripeService.create_setup_intent(user)
    assert result == {"client_secret": "fixture_client_secret", "setup_intent_id": "seti_fixture"}
    assert StripeService.get_or_create_customer(user) == "cus_fixture"
    assert len(responses.calls) == 2
    assert all(call.request.headers["Stripe-Version"] == "2023-10-16" for call in responses.calls)
    assert parse_qs(responses.calls[1].request.body)["customer"] == ["cus_fixture"]
