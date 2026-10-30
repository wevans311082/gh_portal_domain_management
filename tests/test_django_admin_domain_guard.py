from django.urls import reverse
import pytest

from apps.domains.models import Domain
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current, request
from tests.test_bulk_domain_admission import preview, confirm
from tests.security_helpers import login_owner

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(client, django_user_model):
    user = django_user_model.objects.create_user(email="django-admin-owner@example.test", password="testpass")
    login_owner(client, user)
    return user


def data(domain, hosts):
    return {"user": domain.user_id, "name": domain.name, "tld": domain.tld,
            "status": domain.status, "dns_provider": domain.dns_provider,
            "nameservers": "\n".join(hosts), "_save": "Save"}


def test_django_admin_uses_full_list_editor_and_preserves_all_hosts(client, owner, current):
    domain = current[0]
    hosts = [f"ns{i}.example.test" for i in range(1, 14)]
    domain.set_nameserver_cache(hosts)
    domain.save()
    url = reverse("admin:domains_domain_change", args=[domain.pk])
    response = client.get(url)
    assert response.status_code == 200
    form = response.context["adminform"].form
    assert form.initial["nameservers"].splitlines() == hosts
    assert "nameserver1" not in form.fields and "registrar_nameservers" not in form.fields
    response = client.post(url, data(domain, hosts))
    assert response.status_code == 302
    domain.refresh_from_db()
    assert domain.cached_nameservers == hosts and domain.nameserver4 == hosts[3]


@pytest.mark.parametrize("kind", ["bulk", "contact", "submitted-failed"])
def test_django_admin_cannot_edit_admitted_or_uncertain_domain(client, owner, current, kind):
    from django.utils import timezone
    domain = current[0]
    if kind == "bulk":
        confirm(preview(current))
    else:
        child = request(current)
        if kind == "submitted-failed":
            child.status = "failed"
            child.submitted_at = timezone.now()
            child.save(update_fields=["status", "submitted_at"])
    response = client.post(reverse("admin:domains_domain_change", args=[domain.pk]),
        data(domain, ["new1.example.test", "new2.example.test"]))
    assert response.status_code == 302
    assert any("reserved" in str(message) or "requires review" in str(message) or "in progress" in str(message)
               for message in response.wsgi_request._messages)
    domain.refresh_from_db()
    assert domain.registrar_id == "501" and domain.cached_nameservers == []


def test_django_admin_invalid_list_does_not_mutate_cache(client, owner, current):
    domain = current[0]
    response = client.post(reverse("admin:domains_domain_change", args=[domain.pk]), data(domain, ["invalid host", "ns2.example.test"]))
    assert response.status_code == 200
    assert "nameservers" in response.context["adminform"].form.errors
    domain.refresh_from_db()
    assert domain.cached_nameservers == []
