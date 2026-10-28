from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Invoice
from apps.dns.models import DNSRecord, DNSZone
from apps.domains.models import Domain, DomainRenewal, TLDPricing
from apps.portal.cart_service import CartPreflightError, add_domain_renewal_item, create_invoice_from_cart
from tests.test_historical_registration import order, provider, seed

pytestmark = pytest.mark.django_db


@pytest.fixture
def held_domain(order):
    domain = Domain.objects.create(user=order.user, name=order.domain_name, tld="com", status="active", registrar_id="501", epp_code="PRIVATE-CACHED-EPP", auto_renew=False)
    order.domain = domain
    order.save()
    seed()
    return domain


@pytest.mark.parametrize("name", ["toggle_autorenew", "renew", "toggle_lock", "get_auth_code", "update_nameservers"])
def test_customer_provider_actions_stop_before_client_construction(held_domain, client, name):
    client.force_login(held_domain.user)
    with patch("apps.domains.views.ResellerClubClient") as registrar, patch("apps.domains.views.create_invoice") as invoice:
        response = client.post(reverse("domains:" + name, kwargs={"pk": held_domain.pk}), {"nameserver1": "ns1.example.com", "nameserver2": "ns2.example.com"})
    assert response.status_code == 409 and b"Registrar review required" in response.content
    registrar.assert_not_called()
    invoice.assert_not_called()
    assert not DomainRenewal.objects.exists()


def test_renewal_confirmation_get_does_not_offer_payment_while_held(held_domain, client):
    client.force_login(held_domain.user)
    assert client.get(reverse("domains:renew", args=[held_domain.pk])).status_code == 409


def test_local_auto_renew_opt_out_remains_available(held_domain, client):
    held_domain.auto_renew = True
    held_domain.save()
    client.force_login(held_domain.user)
    assert client.post(reverse("domains:toggle_autorenew", args=[held_domain.pk])).status_code == 302
    held_domain.refresh_from_db()
    assert not held_domain.auto_renew


def test_domain_detail_masks_cached_epp_and_does_not_read_providers(held_domain, client):
    client.force_login(held_domain.user)
    with patch("apps.provisioning.whm_client.WHMClient") as whm:
        response = client.get(reverse("domains:detail", args=[held_domain.pk]))
    whm.assert_not_called()
    assert response.status_code == 200 and b"Registrar association awaiting review" in response.content
    assert b"PRIVATE-CACHED-EPP" not in response.content
    held_domain.refresh_from_db()
    assert held_domain.epp_code == "PRIVATE-CACHED-EPP"  # Read masking is not a provider token rotation.


@pytest.mark.parametrize("route", ["zone_detail", "record_add", "record_edit", "record_delete"])
def test_dns_views_hide_cached_records_and_block_sync(held_domain, client, route):
    zone = DNSZone.objects.create(domain=held_domain, provider="registrar")
    record = DNSRecord.objects.create(zone=zone, record_type="TXT", name="@", content="PRIVATE-CACHED-DNS")
    client.force_login(held_domain.user)
    kwargs = {"domain_pk": held_domain.pk}
    if route in {"record_edit", "record_delete"}:
        kwargs["record_pk"] = record.pk
    with patch("apps.dns.views.ensure_zone") as sync:
        response = client.post(reverse("dns:" + route, kwargs=kwargs), {"action": "refresh"})
    assert response.status_code == 409 and b"PRIVATE-CACHED-DNS" not in response.content
    sync.assert_not_called()
    assert DNSRecord.objects.filter(pk=record.pk).exists()


def test_programmatic_dns_import_and_push_also_enforce_the_hold(held_domain):
    from apps.dns.sync import import_records, push_record
    zone = DNSZone.objects.create(domain=held_domain, provider="registrar")
    record = DNSRecord.objects.create(zone=zone, record_type="A", name="@", content="192.0.2.1")
    with pytest.raises(ValidationError, match="requires owner review"):
        import_records(zone)
    with pytest.raises(ValidationError, match="requires owner review"):
        push_record(zone, record)
    zone.refresh_from_db()
    assert zone.last_synced is None


def test_renewal_cart_admission_and_pre_invoice_recheck_enforce_review(order):
    domain = Domain.objects.create(user=order.user, name=order.domain_name, tld="com", status="active", registrar_id="501")
    order.domain = domain
    order.registrar_order_id = "501"
    order.save()
    TLDPricing.objects.create(tld="com", renewal_cost=Decimal("10"))
    item = add_domain_renewal_item(user=order.user, domain_id=domain.pk)
    seed()
    with pytest.raises(CartPreflightError, match="requires owner review"):
        add_domain_renewal_item(user=order.user, domain_id=domain.pk)
    with pytest.raises(CartPreflightError, match="requires owner review"):
        create_invoice_from_cart(item.cart)
    assert not Invoice.objects.exists()


def test_paid_queued_renewal_cannot_charge_the_unconfirmed_registrar_target(held_domain, provider):
    from apps.domains.tasks import execute_domain_renewal
    invoice = Invoice.objects.create(user=held_domain.user, number="HELD-RENEWAL", status="paid", total=10)
    renewal = DomainRenewal.objects.create(domain=held_domain, user=held_domain.user, invoice=invoice, status="paid")
    with patch("apps.domains.tasks.ResellerClubClient", return_value=provider) as construct:
        execute_domain_renewal.run(renewal.pk)
    construct.assert_not_called()
    renewal.refresh_from_db()
    assert renewal.status == "failed" and "requires owner review" in renewal.last_error


def test_auto_renew_scheduler_skips_held_domain_before_allocating_an_invoice(held_domain):
    from apps.domains.tasks import process_auto_renewals
    held_domain.auto_renew = True
    held_domain.expires_at = timezone.localdate() + timezone.timedelta(days=2)
    held_domain.save()
    assert process_auto_renewals.run() == 0
    assert not Invoice.objects.exists() and not DomainRenewal.objects.exists()


def test_configured_account_change_blocks_existing_cart_and_verification(order):
    from apps.domains.verification import registrar_email_summary
    from apps.domains.verification_actions import request_verification_action
    import uuid

    domain = Domain.objects.create(user=order.user, name=order.domain_name, tld="com", registrar_id="501")
    order.domain = domain
    order.registrar_order_id = "501"
    order.registrar_contact_ids = {"_scope": "original-account", "reg": "123"}
    order.save()
    TLDPricing.objects.create(tld="com", renewal_cost=Decimal("10"))
    with patch("apps.domains.contact_bindings.provider_scope", return_value="original-account"):
        item = add_domain_renewal_item(user=order.user, domain_id=domain.pk)
    with patch("apps.domains.contact_bindings.provider_scope", return_value="replacement-account"):
        with pytest.raises(CartPreflightError, match="requires owner review"):
            create_invoice_from_cart(item.cart)
        with pytest.raises(ValidationError, match="association review"):
            request_verification_action(domain, domain.user, "refresh", uuid.uuid4())
        assert registrar_email_summary(domain)["label"] == "Registrar association review required"
    assert not Invoice.objects.exists()
