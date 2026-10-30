import pytest

from apps.admin_tools.resellerclub_views import _apply_nameservers_from_order
from apps.domains.models import Domain

pytestmark = pytest.mark.django_db


@pytest.fixture
def domain(django_user_model):
    user = django_user_model.objects.create_user(email="ns-import@example.test", password="testpass")
    domain = Domain.objects.create(user=user, name="cache.com", tld="com")
    domain.set_nameserver_cache([f"old{i}.example.test" for i in range(1, 14)])
    domain.save()
    return domain


@pytest.mark.parametrize("representation", ["numbered", "array", "both"])
def test_import_preserves_all_thirteen_hosts_and_mirrors_legacy_fields(domain, representation):
    hosts = [f"ns{i}.example.test" for i in range(1, 14)]
    details = {"noOfNameServers": "13", **{f"ns{i}": host for i, host in enumerate(hosts, 1)}}
    if representation == "array":
        details = {"nameservers": hosts}
    elif representation == "both":
        details["nameservers"] = hosts
    assert _apply_nameservers_from_order(domain, details) is True
    domain.refresh_from_db()
    assert domain.cached_nameservers == hosts
    assert [getattr(domain, f"nameserver{i}") for i in range(1, 5)] == hosts[:4]


@pytest.mark.parametrize("details", [
    {}, {"ns1": "one.example.test", "ns2": "two.example.test"},
    {"noOfNameServers": "13", "ns1": "one.example.test", "ns2": "two.example.test"},
    {"nameservers": "ns1.example.test,ns2.example.test"},
    {"nameservers": [True, "two.example.test"]},
    {"nameservers": ["same.example.test", "same.example.test"]},
    {"nameservers": ["invalid host", "two.example.test"]},
    {"nameservers": ["one.example.test", "two.example.test"], "noOfNameServers": True},
    {"nameservers": ["one.example.test", "two.example.test"], "noOfNameServers": 3, "ns3": "three.example.test"},
    {"nameservers": ["one.example.test", "two.example.test"], "ns1": "different.example.test"},
    {"nameservers": ["one.example.test", "two.example.test"], "ns14": "extra.example.test"},
    {"nameservers": ["one.example.test", "two.example.test"], "ns01": "extra.example.test"},
    {"nameservers": [f"ns{i}.example.test" for i in range(14)]},
    {"ns": [{"ns": "one.example.test"}, {"ns": "two.example.test"}]},
])
def test_partial_malformed_or_conflicting_inventory_preserves_existing_complete_cache(domain, details):
    before = domain.cached_nameservers
    assert _apply_nameservers_from_order(domain, details) is False
    domain.refresh_from_db()
    assert domain.cached_nameservers == before
    assert [getattr(domain, f"nameserver{i}") for i in range(1, 5)] == before[:4]


def test_complete_smaller_set_clears_stale_legacy_suffix(domain):
    assert _apply_nameservers_from_order(domain, {"nameservers": ["NS1.EXAMPLE.TEST.", "ns2.example.test"]}) is True
    domain.refresh_from_db()
    assert domain.cached_nameservers == ["ns1.example.test", "ns2.example.test"]
    assert domain.nameserver3 == domain.nameserver4 == ""
