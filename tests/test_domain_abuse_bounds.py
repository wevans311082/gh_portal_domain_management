"""Bound public registrar fan-out and reject invalid searches before I/O."""
import pytest
from django.urls import reverse
from django.core.cache import cache

from apps.domains.models import DomainPricingSettings, TLDPricing

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("parameters", [
    {"q": "x" * 254}, {"q": "example.com/path"}, {"q": "example.unsupported"},
    {"q": "example..com"}, {"q": "example", "tlds": ",".join(["com"] * 11)},
    {"q": "example", "tlds": "com,,net"}, {"q": "example", "tlds": "unknown"},
    {"q": "example", "tlds": "com/../../"},
])
def test_invalid_search_never_reaches_registrar(client, monkeypatch, parameters):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid public search reached provider work")
    monkeypatch.setattr("apps.domains.views.ResellerClubClient", forbidden)
    monkeypatch.setattr("apps.domains.views._sync_missing_tld_pricing", forbidden)
    assert client.get(reverse("domains:check"), parameters).status_code == 400


def test_default_fanout_is_capped_but_full_supported_domain_still_works(client, monkeypatch):
    configured = DomainPricingSettings.get_solo()
    configured.supported_tlds = [f"tld{index}" for index in range(25)]
    configured.save()
    calls = []
    class Provider:
        def check_availability(self, labels, tlds):
            calls.append(list(tlds))
            return {}
    monkeypatch.setattr("apps.domains.views.ResellerClubClient", Provider)
    monkeypatch.setattr("apps.domains.views._sync_missing_tld_pricing", lambda *args: None)
    response = client.get(reverse("domains:check"), {"q": "bounded"})
    assert response.status_code == 200
    assert len(calls) == 1 and len(calls[0]) == 10
    assert len(response.context["results"]) == 10
    assert b"first 10 supported extensions" in response.content
    response = client.get(reverse("domains:check"), {"q": "bounded.tld24"})
    assert response.status_code == 200
    assert calls[-1] == ["tld24"]


def test_search_page_pricing_sync_is_bounded(client, monkeypatch):
    monkeypatch.setattr("apps.domains.views.get_active_tlds", lambda: [f"tld{index}" for index in range(100)])
    calls = []
    monkeypatch.setattr("apps.domains.views._sync_missing_tld_pricing", lambda tlds: calls.append(tlds))
    response = client.get(reverse("domains:search"))
    assert response.status_code == 200
    assert len(calls[0]) == len(response.context["popular_tlds"]) == 10


@pytest.mark.parametrize("actor", ["anonymous", "staff", "stale-owner"])
def test_debug_flag_cannot_force_unverified_live_pricing(client, monkeypatch, django_user_model, actor):
    if actor == "staff":
        client.force_login(django_user_model.objects.create_user(email="domain-staff@example.test", is_staff=True))
    elif actor == "stale-owner":
        from tests.security_helpers import login_owner
        login_owner(client, django_user_model.objects.create_user(email="domain-stale-owner@example.test"))
        session = client.session
        session["_mfa_verified_at"] = 0
        session.save()
    configured = DomainPricingSettings.get_solo()
    configured.supported_tlds = ["com"]
    configured.save()
    TLDPricing.objects.create(tld="com", registration_cost="10", renewal_cost="10", transfer_cost="10")
    cache.set("domain_avail:cached.com", False, 60)
    monkeypatch.setattr("apps.domains.views.get_runtime_setting", lambda key, default="": "true" if key == "RESELLERCLUB_DEBUG_MODE" else default)
    monkeypatch.setattr("apps.domains.views.ResellerClubClient", lambda: pytest.fail("Unverified debug bypassed the cache"))
    response = client.get(reverse("domains:check"), {"q": "cached.com"})
    assert response.status_code == 200 and not response.context["debug_mode"]


def test_debug_provider_initialization_failure_returns_unknown_results(client, django_user_model, monkeypatch):
    from tests.security_helpers import login_owner
    from apps.domains.resellerclub_client import ResellerClubError
    login_owner(client, django_user_model.objects.create_user(email="domain-failure-owner@example.test"))
    configured = DomainPricingSettings.get_solo()
    configured.supported_tlds = ["com"]
    configured.save()
    monkeypatch.setattr("apps.domains.views.get_runtime_setting", lambda key, default="": "true" if key == "RESELLERCLUB_DEBUG_MODE" else default)
    def unavailable():
        raise ResellerClubError("Provider unavailable")
    monkeypatch.setattr("apps.domains.views.ResellerClubClient", unavailable)
    response = client.get(reverse("domains:check"), {"q": "unavailable.com"})
    assert response.status_code == 200
    assert response.context["results"][0]["available"] is None


@pytest.mark.parametrize("domain", ["x" * 254 + ".com", "example.com/path", "example.com:80", "example..com"])
def test_whois_rejects_invalid_names_before_network(client, monkeypatch, domain):
    monkeypatch.setattr("apps.domains.views.requests.get", lambda *args, **kwargs: pytest.fail("Invalid WHOIS reached network"))
    assert client.get(reverse("domains:whois"), {"domain": domain}).status_code == 400


def test_concurrent_missing_price_sync_has_one_admitted_caller(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from apps.domains.views import _sync_missing_tld_pricing
    calls = []
    class Pricing:
        def sync_pricing(self, tlds):
            calls.append(tlds)
    monkeypatch.setattr("apps.domains.views.TLDPricingService", Pricing)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: _sync_missing_tld_pricing(["atomic-test"]), range(20)))
    assert calls == [["atomic-test"]]
