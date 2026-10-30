from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError

from apps.domains.models import Domain
from apps.domains.tasks import _ensure_cloudflare_zone

pytestmark = pytest.mark.django_db


@pytest.fixture
def domain(django_user_model):
    user = django_user_model.objects.create_user(email="platform-ns@example.test", password="testpass")
    domain = Domain.objects.create(user=user, name="platform.com", tld="com", registrar_id="501")
    domain.set_nameserver_cache([f"old{i}.example.test" for i in range(1, 14)])
    domain.save()
    return domain


def provision(domain, hosts, registrar):
    cloud = Mock()
    cloud.create_zone.return_value = {"result": {"id": "zone-1", "name_servers": hosts}}
    cloud.create_dns_record.return_value = {"result": {"id": "record-1"}}
    with patch("apps.domains.tasks.CloudflareService", return_value=cloud):
        _ensure_cloudflare_zone(domain, SimpleNamespace(registrar_order_id="501"), registrar)
    return cloud


@pytest.mark.parametrize("count", [2, 13])
def test_platform_delegation_replaces_full_cache_and_clears_stale_suffix(domain, count):
    hosts = [f"ns{i}.example.test" for i in range(1, count + 1)]
    registrar = Mock()
    provision(domain, hosts, registrar)
    registrar.modify_nameservers.assert_called_once_with("501", hosts)
    domain.refresh_from_db()
    assert domain.cached_nameservers == hosts
    assert [getattr(domain, f"nameserver{i}") for i in range(1, 5)] == (hosts + [""] * 4)[:4]


@pytest.mark.parametrize("hosts", [
    ["same.example.test", "same.example.test"],
    ["invalid host", "ns2.example.test"],
    [f"ns{i}.example.test" for i in range(14)],
    "ns1.example.test,ns2.example.test",
])
def test_invalid_platform_delegation_never_reaches_registrar_or_replaces_cache(domain, hosts):
    before = domain.cached_nameservers
    registrar = Mock()
    with pytest.raises(ValidationError):
        provision(domain, hosts, registrar)
    registrar.modify_nameservers.assert_not_called()
    domain.refresh_from_db()
    assert domain.cached_nameservers == before


def test_uncertain_registrar_result_preserves_existing_cache(domain):
    before = domain.cached_nameservers
    registrar = Mock()
    registrar.modify_nameservers.side_effect = TimeoutError("private provider diagnostic")
    with pytest.raises(TimeoutError):
        provision(domain, ["ns1.example.test", "ns2.example.test"], registrar)
    domain.refresh_from_db()
    assert domain.cached_nameservers == before
