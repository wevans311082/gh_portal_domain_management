from unittest.mock import Mock, patch

import pytest
from django.urls import reverse

from apps.domains.resellerclub_client import ResellerClubError
from tests.test_domain_self_service import make_domain, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def owned(client, django_user_model):
    user = make_user(django_user_model)
    domain = make_domain(user)
    domain.set_nameserver_cache([f"ns{i}.example.test" for i in range(1, 14)])
    domain.save()
    client.force_login(user)
    return domain


def test_customer_detail_initializes_complete_nameserver_list(client, owned):
    with patch("apps.provisioning.whm_client.WHMClient"):
        response = client.get(reverse("domains:detail", args=[owned.pk]))
    assert response.status_code == 200
    assert response.context["nameserver_form"].initial["nameservers"].splitlines() == owned.cached_nameservers
    assert b'name="nameserver1"' not in response.content


@pytest.mark.parametrize("count", [2, 13])
def test_customer_submits_whole_set_and_updates_cache_consistently(client, owned, count):
    hosts = [f"new{i}.example.test" for i in range(1, count + 1)]
    provider = Mock()
    with patch("apps.domains.views.ResellerClubClient", return_value=provider):
        response = client.post(reverse("domains:update_nameservers", args=[owned.pk]), {"nameservers": "\n".join(hosts)})
    assert response.status_code == 302
    provider.modify_nameservers.assert_called_once_with(owned.registrar_id, hosts)
    owned.refresh_from_db()
    assert owned.cached_nameservers == hosts
    assert [getattr(owned, f"nameserver{i}") for i in range(1, 5)] == (hosts + [""] * 4)[:4]


@pytest.mark.parametrize("value", ["bad host\nns2.example.test", "same.example.test\nsame.example.test", "ns1.example.test",
    "\n".join(f"ns{i}.example.test" for i in range(14)), ""])
def test_customer_invalid_list_is_retained_and_never_sent(client, owned, value):
    with patch("apps.domains.views.ResellerClubClient") as provider, patch("apps.provisioning.whm_client.WHMClient"):
        response = client.post(reverse("domains:update_nameservers", args=[owned.pk]), {"nameservers": value})
    assert response.status_code == 400 and response["Cache-Control"] == "no-store"
    assert response.context["nameserver_form"].data["nameservers"] == value
    assert "nameservers" in response.context["nameserver_form"].errors
    provider.assert_not_called()
    owned.refresh_from_db()
    assert len(owned.cached_nameservers) == 13


def test_legacy_four_field_submission_cannot_silently_truncate_known_full_list(client, owned):
    with patch("apps.domains.views.ResellerClubClient") as provider:
        response = client.post(reverse("domains:update_nameservers", args=[owned.pk]), {"nameserver1": "new1.example.test", "nameserver2": "new2.example.test"})
    assert response.status_code == 302
    provider.assert_not_called()
    owned.refresh_from_db()
    assert len(owned.cached_nameservers) == 13


def test_registrar_failure_does_not_replace_customer_cache(client, owned):
    provider = Mock()
    provider.modify_nameservers.side_effect = ResellerClubError("provider unavailable")
    with patch("apps.domains.views.ResellerClubClient", return_value=provider):
        response = client.post(reverse("domains:update_nameservers", args=[owned.pk]), {"nameservers": "new1.example.test\nnew2.example.test"})
    assert response.status_code == 302
    owned.refresh_from_db()
    assert len(owned.cached_nameservers) == 13


def test_repeated_complete_list_fields_are_rejected(client, owned):
    with patch("apps.domains.views.ResellerClubClient") as provider, patch("apps.provisioning.whm_client.WHMClient"):
        response = client.post(reverse("domains:update_nameservers", args=[owned.pk]), {
            "nameservers": ["one.example.test\ntwo.example.test", "three.example.test\nfour.example.test"]})
    assert response.status_code == 400
    assert "Submit one complete" in str(response.context["nameserver_form"].errors)
    provider.assert_not_called()


def test_complete_list_cannot_update_another_customers_domain(client, owned, django_user_model):
    other = make_user(django_user_model)
    client.force_login(other)
    with patch("apps.domains.views.ResellerClubClient") as provider:
        response = client.post(reverse("domains:update_nameservers", args=[owned.pk]), {"nameservers": "one.example.test\ntwo.example.test"})
    assert response.status_code == 404
    provider.assert_not_called()
