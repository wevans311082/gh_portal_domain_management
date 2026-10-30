from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.admin_tools.resellerclub_views import _upsert_domain, _inventory_association, _apply_nameservers_from_order
from apps.domains.models import Domain
from apps.domains.resellerclub_client import ResellerClubClient, merge_order_details
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current
from tests.test_inventory_operation_guard import payload
from tests.security_helpers import login_owner

pytestmark = pytest.mark.django_db


def test_automatic_inventory_cannot_restore_stale_owner(current, django_user_model):
    domain = current[0]
    old_user = domain.user
    replacement = django_user_model.objects.create_user(email="fresh-owner@example.test", password="testpass")
    Domain.objects.filter(pk=domain.pk).update(user=replacement)
    with pytest.raises(ValidationError, match="cannot reassign"):
        _upsert_domain(payload(domain), old_user)
    domain.refresh_from_db()
    assert domain.user_id == replacement.pk and domain.registrar_id == "501"


def test_explicit_inventory_reassignment_remains_available_without_active_work(current, django_user_model):
    replacement = django_user_model.objects.create_user(email="explicit-owner@example.test", password="testpass")
    result, created = _upsert_domain(payload(current[0]), replacement, allow_reassignment=True)
    assert not created and result.user_id == replacement.pk


def test_single_sync_rejects_stale_association_inside_transaction(current):
    domain = current[0]
    expected = _inventory_association(domain)
    Domain.objects.filter(pk=domain.pk).update(registrar_id="777")
    with pytest.raises(ValidationError, match="association changed"):
        _upsert_domain(payload(domain), domain.user, expected_association=expected)
    domain.refresh_from_db()
    assert domain.registrar_id == "777" and domain.status == "active"


@pytest.mark.parametrize("changes", [{"domainname": "another.com"}, {"orderid": "222"}])
@pytest.mark.parametrize("action", ["sync_order", "import_order"])
def test_http_inventory_rejects_mismatched_provider_identity(client, current, django_user_model, changes, action):
    domain = current[0]
    owner = django_user_model.objects.create_user(email="identity-owner@example.test", password="testpass")
    login_owner(client, owner)
    observed = {**payload(domain), "orderid": domain.registrar_id, **changes}
    with patch("apps.admin_tools.resellerclub_views.ResellerClubClient") as provider, patch("apps.admin_tools.resellerclub_views.WHMClient"):
        provider.return_value.list_all_domain_orders.return_value = [observed]
        provider.return_value.list_customers.return_value = []
        provider.return_value.get_order_details.return_value = observed
        provider.return_value._normalize_order_record.return_value = observed
        response = client.post(reverse("admin_tools:resellerclub_hub"), {"action": action,
            "domain_id": domain.pk, "domain_name": domain.name, "order_id": domain.registrar_id, "user_id": domain.user_id})
    assert response.status_code == 302
    assert any("does not match" in str(message) for message in response.wsgi_request._messages)
    domain.refresh_from_db()
    assert domain.registrar_id == "501" and domain.status == "active"
    assert not Domain.objects.filter(name="another.com").exists()


@pytest.mark.parametrize("shape", ["numbered", "array"])
def test_actual_normalizer_and_cache_parser_retain_all_thirteen_hosts(current, shape):
    domain = current[0]
    hosts = [f"ns{i}.example.test" for i in range(1, 14)]
    details = {"nameservers": hosts} if shape == "array" else {
        "noOfNameServers": "13", **{f"ns{i}": host for i, host in enumerate(hosts, 1)}}
    normalized = ResellerClubClient()._normalize_order_record(details)
    assert _apply_nameservers_from_order(domain, normalized) is True
    domain.refresh_from_db()
    assert domain.cached_nameservers == hosts


@pytest.mark.parametrize("details", [
    {"ns1": "one.example.test", "ns2": "two.example.test"},
    {"nameservers": ["same.example.test", "same.example.test"]},
    {"noOfNameServers": "13", "ns1": "one.example.test", "ns2": "two.example.test"},
])
def test_normalization_does_not_promote_prefixes_or_hide_invalid_sets(current, details):
    domain = current[0]
    domain.set_nameserver_cache([f"old{i}.example.test" for i in range(1, 14)])
    domain.save()
    normalized = ResellerClubClient()._normalize_order_record(details)
    assert _apply_nameservers_from_order(domain, normalized) is False
    domain.refresh_from_db()
    assert len(domain.cached_nameservers) == 13 and domain.nameserver1 == "old1.example.test"


def test_repeated_normalization_cannot_promote_a_synthesized_prefix(current):
    provider = ResellerClubClient()
    prefix = provider._normalize_order_record({"ns1": "one.example.test", "ns2": "two.example.test"})
    repeated = provider._normalize_order_record(prefix)
    assert repeated["_nameserver_set_complete"] is False
    assert _apply_nameservers_from_order(current[0], repeated) is False


@pytest.mark.parametrize("complete", [True, False])
def test_fresh_detail_delegation_replaces_old_transport_fields_without_union(current, complete):
    provider = ResellerClubClient()
    old = provider._normalize_order_record({"noOfNameServers": 2, "ns1": "old1.example.test", "ns2": "old2.example.test"})
    hosts = [f"new{i}.example.test" for i in range(1, 14)]
    details = {"nameservers": hosts} if complete else {"ns1": hosts[0], "ns2": hosts[1]}
    normalized = provider._normalize_order_record(merge_order_details(old, details))
    assert _apply_nameservers_from_order(current[0], normalized) is complete
    current[0].refresh_from_db()
    assert current[0].cached_nameservers == (hosts if complete else [])
