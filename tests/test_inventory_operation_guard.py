from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.admin_tools.resellerclub_views import _upsert_domain
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current, request
from tests.test_bulk_domain_admission import preview, confirm
from tests.security_helpers import login_owner

pytestmark = pytest.mark.django_db


def payload(domain):
    return {"domainname": domain.name, "orderid": "9999", "currentstatus": "Suspended",
            "nameservers": ["changed1.example.test", "changed2.example.test"], "expiry_date": "2035-01-01"}


def hold(current, kind):
    if kind == "bulk":
        batch = preview(current)
        confirm(batch)
    else:
        change = request(current)
        if kind == "uncertain-failed":
            change.status = "failed"
            change.submitted_at = timezone.now()
            change.save(update_fields=["status", "submitted_at"])


@pytest.mark.parametrize("kind", ["bulk", "contact", "uncertain-failed"])
def test_inventory_update_cannot_reassociate_or_refresh_an_admitted_domain(current, django_user_model, kind):
    domain = current[0]
    hold(current, kind)
    replacement = django_user_model.objects.create_user(email="replacement@example.test", password="testpass")
    with pytest.raises(ValidationError):
        _upsert_domain(payload(domain), replacement)
    domain.refresh_from_db()
    assert domain.user_id == current[1].user_id and domain.registrar_id == "501"
    assert domain.status == "active" and domain.expires_at is None
    assert domain.cached_nameservers == []


def test_unreserved_inventory_update_retains_valid_full_cache(current):
    domain = current[0]
    data = payload(domain)
    data["nameservers"] = [f"ns{i}.example.test" for i in range(1, 14)]
    result, created = _upsert_domain(data, domain.user)
    assert not created and result.pk == domain.pk
    domain.refresh_from_db()
    assert domain.registrar_id == "9999" and len(domain.cached_nameservers) == 13


def test_inventory_can_create_an_unreserved_domain_with_complete_cache(current):
    data = payload(current[0])
    data["domainname"] = "fresh-inventory.com"
    data["nameservers"] = [f"ns{i}.example.test" for i in range(1, 14)]
    domain, created = _upsert_domain(data, current[0].user)
    assert created and domain.user_id == current[0].user_id
    domain.refresh_from_db()
    assert len(domain.cached_nameservers) == 13


def test_inventory_cache_persistence_failure_rolls_back_association_metadata(current):
    domain = current[0]
    with patch("apps.admin_tools.resellerclub_views._apply_nameservers_from_order", side_effect=RuntimeError("local persistence failure")):
        with pytest.raises(RuntimeError):
            _upsert_domain(payload(domain), domain.user)
    domain.refresh_from_db()
    assert domain.registrar_id == "501" and domain.status == "active" and domain.expires_at is None


@pytest.mark.parametrize("action", ["import", "sync", "link"])
def test_inventory_http_operations_explain_hold_without_success_or_mutation(client, current, django_user_model, action):
    domain = current[0]
    hold(current, "bulk")
    owner = django_user_model.objects.create_user(email="inventory-owner@example.test", password="testpass")
    login_owner(client, owner)
    target = django_user_model.objects.create_user(email="inventory-target@example.test", password="testpass")
    # Use the hub's actual action identifiers rather than bypassing authorization.
    actions = {"import": "import_order", "sync": "sync_order", "link": "link_user"}
    observed = payload(domain)
    if action == "sync":
        observed["orderid"] = domain.registrar_id
    with patch("apps.admin_tools.resellerclub_views.ResellerClubClient") as provider, patch(
        "apps.admin_tools.resellerclub_views.WHMClient"
    ):
        provider.return_value.list_all_domain_orders.return_value = [observed]
        provider.return_value.list_customers.return_value = []
        provider.return_value.get_order_details.return_value = observed
        provider.return_value._normalize_order_record.return_value = observed
        response = client.post(reverse("admin_tools:resellerclub_hub"), {
            "action": actions[action], "domain_id": domain.pk, "domain_name": domain.name,
            "order_id": "9999", "user_id": target.pk,
        })
    assert response.status_code == 302
    assert any("reserved" in str(message) for message in response.wsgi_request._messages)
    domain.refresh_from_db()
    assert domain.user_id == current[1].user_id and domain.registrar_id == "501"
