from decimal import Decimal

import pytest
from django.urls import reverse
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.domains.models import DomainPricingSettings, TLDPricing
from apps.domains.pricing import TLDPricingService
from apps.domains.tasks import ensure_tld_pricing_sync_schedule, sync_tld_pricing


@pytest.mark.django_db
def test_tld_pricing_uses_default_margin_percentage():
    settings_obj = DomainPricingSettings.get_solo()
    settings_obj.default_profit_margin_percentage = Decimal("25.00")
    settings_obj.save(update_fields=["default_profit_margin_percentage"])
    pricing = TLDPricing.objects.create(tld="com", registration_cost=Decimal("8.00"), renewal_cost=Decimal("9.00"))

    assert pricing.registration_price == Decimal("10.00")
    assert pricing.renewal_price == Decimal("11.25")


@pytest.mark.django_db
def test_tld_pricing_override_margin_takes_precedence():
    settings_obj = DomainPricingSettings.get_solo()
    settings_obj.default_profit_margin_percentage = Decimal("25.00")
    settings_obj.save(update_fields=["default_profit_margin_percentage"])
    pricing = TLDPricing.objects.create(
        tld="io",
        registration_cost=Decimal("20.00"),
        profit_margin_percentage=Decimal("10.00"),
    )

    assert pricing.registration_price == Decimal("22.00")


@pytest.mark.django_db
def test_pricing_service_syncs_records(monkeypatch):
    settings_obj = DomainPricingSettings.get_solo()
    settings_obj.supported_tlds = ["com", "co.uk"]
    settings_obj.save(update_fields=["supported_tlds"])

    class FakeClient:
        def get_tld_costs(self, tld, years=1):
            base = Decimal("9.50") if tld == "com" else Decimal("6.75")
            return {
                "registration": {"price": base},
                "renewal": {"customer_price": base + Decimal("1.00")},
                "transfer": {"amount": base + Decimal("2.00")},
            }

    synced = TLDPricingService(client=FakeClient()).sync_pricing()

    assert len(synced) == 2
    assert TLDPricing.objects.get(tld="com").registration_cost == Decimal("9.50")
    assert TLDPricing.objects.get(tld="co.uk").transfer_cost == Decimal("8.75")


@pytest.mark.django_db
def test_pricing_service_syncs_realistic_camelcase_payloads():
    settings_obj = DomainPricingSettings.get_solo()
    settings_obj.supported_tlds = ["com"]
    settings_obj.save(update_fields=["supported_tlds"])

    class FakeClient:
        def get_tld_costs(self, tld, years=1):
            return {
                "registration": {
                    "description": "ok",
                    "customerPrice": "12.34",
                    "isPremiumDomain": False,
                },
                "renewal": {
                    "sellingCurrencyAmount": "13.45",
                    "actionstatus": "Success",
                },
                "transfer": {
                    "resellerPrice": "14.56",
                },
            }

    synced = TLDPricingService(client=FakeClient()).sync_pricing()

    assert len(synced) == 1
    pricing = TLDPricing.objects.get(tld="com")
    assert pricing.registration_cost == Decimal("12.34")
    assert pricing.renewal_cost == Decimal("13.45")
    assert pricing.transfer_cost == Decimal("14.56")


@pytest.mark.django_db
def test_sync_schedule_uses_pricing_settings_interval():
    settings_obj = DomainPricingSettings.get_solo()
    settings_obj.sync_interval_hours = 12
    settings_obj.sync_enabled = True
    settings_obj.save(update_fields=["sync_interval_hours", "sync_enabled"])

    task = ensure_tld_pricing_sync_schedule(settings_obj)

    assert task.task == "apps.domains.tasks.sync_tld_pricing"
    assert task.enabled is True
    assert task.interval.every == 12
    assert task.interval.period == IntervalSchedule.HOURS


@pytest.mark.django_db
def test_sync_tld_pricing_task_updates_records(monkeypatch):
    settings_obj = DomainPricingSettings.get_solo()
    settings_obj.supported_tlds = ["org"]
    settings_obj.save(update_fields=["supported_tlds"])

    class FakeService:
        def sync_pricing(self, tlds=None, years=1):
            TLDPricing.objects.update_or_create(
                tld="org",
                defaults={
                    "registration_cost": Decimal("7.00"),
                    "renewal_cost": Decimal("8.00"),
                    "transfer_cost": Decimal("9.00"),
                },
            )
            return list(TLDPricing.objects.filter(tld="org"))

    monkeypatch.setattr("apps.domains.tasks.TLDPricingService", lambda: FakeService())

    synced_count = sync_tld_pricing.apply(kwargs={"tlds": ["org"]}).get()

    assert synced_count == 1
    assert TLDPricing.objects.get(tld="org").registration_cost == Decimal("7.00")
    assert PeriodicTask.objects.get(name="Sync TLD pricing").enabled is True


@pytest.mark.django_db
def test_domain_check_renders_cached_sell_price(client, monkeypatch):
    settings_obj = DomainPricingSettings.get_solo()
    settings_obj.default_profit_margin_percentage = Decimal("20.00")
    settings_obj.save(update_fields=["default_profit_margin_percentage"])
    TLDPricing.objects.create(tld="com", registration_cost=Decimal("10.00"))

    class FakeClient:
        def check_availability(self, domain_names, tlds):
            full_domain = f"{domain_names[0]}.{tlds[0]}"
            return {full_domain: {"status": "available"}}

    monkeypatch.setattr("apps.domains.views.ResellerClubClient", lambda: FakeClient())

    response = client.get(reverse("domains:check"), {"q": "example"})
    content = response.content.decode()

    assert response.status_code == 200
    assert "GBP 12.00/yr" in content


@pytest.mark.django_db
def test_domain_check_shows_transfer_price_and_whois_for_taken_domain(client, monkeypatch):
    settings_obj = DomainPricingSettings.get_solo()
    settings_obj.default_profit_margin_percentage = Decimal("25.00")
    settings_obj.save(update_fields=["default_profit_margin_percentage"])
    TLDPricing.objects.create(
        tld="com",
        registration_cost=Decimal("8.00"),
        renewal_cost=Decimal("9.00"),
        transfer_cost=Decimal("10.00"),
    )

    class FakeClient:
        def check_availability(self, domain_names, tlds):
            full_domain = f"{domain_names[0]}.{tlds[0]}"
            return {full_domain: {"status": "regthroughothers"}}

    monkeypatch.setattr("apps.domains.views.ResellerClubClient", lambda: FakeClient())

    response = client.get(reverse("domains:check"), {"q": "takenexample"})
    content = response.content.decode()

    assert response.status_code == 200
    assert "Taken" in content
    assert "Transfer GBP 12.50/yr" in content
    assert "lookup.icann.org" in content
    assert "Transfer In" in content


@pytest.mark.django_db
def test_domain_check_syncs_missing_pricing_records(client, monkeypatch):
    class FakePricingService:
        def sync_pricing(self, tlds=None, years=1):
            for tld in tlds or []:
                TLDPricing.objects.update_or_create(
                    tld=tld,
                    defaults={
                        "registration_cost": Decimal("10.00"),
                        "renewal_cost": Decimal("11.00"),
                        "transfer_cost": Decimal("12.00"),
                        "is_active": True,
                    },
                )
            return list(TLDPricing.objects.filter(tld__in=(tlds or [])))

    class FakeClient:
        def check_availability(self, domain_names, tlds):
            full_domain = f"{domain_names[0]}.{tlds[0]}"
            return {full_domain: {"status": "available"}}

    monkeypatch.setattr("apps.domains.views.TLDPricingService", lambda: FakePricingService())
    monkeypatch.setattr("apps.domains.views.ResellerClubClient", lambda: FakeClient())

    response = client.get(reverse("domains:check"), {"q": "freshsync"})
    content = response.content.decode()

    assert response.status_code == 200
    assert TLDPricing.objects.filter(tld="com").exists()
    assert "Registration GBP 12.50/yr" in content