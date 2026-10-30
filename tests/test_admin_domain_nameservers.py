import pytest

from apps.admin_tools.forms import DomainForm
from apps.domains.models import Domain
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db


@pytest.fixture
def domain(django_user_model):
    user = django_user_model.objects.create_user(email="admin-ns@example.test", password="testpass")
    domain = Domain.objects.create(user=user, name="admin-cache.com", tld="com")
    domain.set_nameserver_cache([f"ns{i}.example.test" for i in range(1, 14)])
    domain.save()
    return domain


def data(domain, hosts):
    return {"user": domain.user_id, "name": domain.name, "tld": domain.tld,
            "status": domain.status, "dns_provider": domain.dns_provider, "nameservers": hosts}


def test_admin_editor_displays_and_retains_entire_cache(domain):
    form = DomainForm(instance=domain)
    assert form.initial["nameservers"].splitlines() == domain.cached_nameservers
    assert "nameserver1" not in form.fields
    form = DomainForm(data(domain, form.initial["nameservers"]), instance=domain)
    assert form.is_valid(), form.errors
    form.save()
    domain.refresh_from_db()
    assert len(domain.cached_nameservers) == 13 and domain.nameserver4 == "ns4.example.test"


def test_admin_editor_normalizes_and_clears_legacy_suffix_without_early_save(domain):
    form = DomainForm(data(domain, "NS1.EXAMPLE.TEST.\n\nns2.example.test"), instance=domain)
    assert form.is_valid(), form.errors
    result = form.save(commit=False)
    assert len(Domain.objects.get(pk=domain.pk).cached_nameservers) == 13
    result.save()
    domain.refresh_from_db()
    assert domain.cached_nameservers == ["ns1.example.test", "ns2.example.test"]
    assert domain.nameserver3 == domain.nameserver4 == ""


@pytest.mark.parametrize("hosts", ["bad host\nns2.example.test", "same.example.test\nsame.example.test",
    "\n".join(f"ns{i}.example.test" for i in range(14)), "ns1.example.test"])
def test_invalid_admin_cache_edit_preserves_saved_complete_set(domain, hosts):
    form = DomainForm(data(domain, hosts), instance=domain)
    assert not form.is_valid() and "nameservers" in form.errors
    domain.refresh_from_db()
    assert len(domain.cached_nameservers) == 13


def test_empty_admin_cache_does_not_leave_hidden_legacy_hosts(domain):
    form = DomainForm(data(domain, ""), instance=domain)
    assert form.is_valid(), form.errors
    form.save()
    domain.refresh_from_db()
    assert domain.cached_nameservers == []
    assert all(getattr(domain, f"nameserver{i}") == "" for i in range(1, 5))


def test_admin_http_edit_retains_full_list(client, domain, django_user_model):
    from django.urls import reverse
    from tests.security_helpers import login_owner
    actor = django_user_model.objects.create_user(email="ns-owner@example.test", password="testpass", is_staff=True)
    login_owner(client, actor)
    response = client.post(reverse("admin_tools:domains_edit", args=[domain.pk]), data(domain, "\n".join(domain.cached_nameservers)))
    assert response.status_code == 302
    domain.refresh_from_db()
    assert len(domain.cached_nameservers) == 13


@pytest.mark.parametrize("operation", ["bulk", "contact"])
def test_admin_http_edit_cannot_change_reserved_domain(client, current, django_user_model, operation):
    from django.urls import reverse
    from tests.security_helpers import login_owner
    from tests.test_bulk_domain_admission import preview, confirm
    domain = current[0]
    if operation == "bulk":
        batch = preview(current)
        confirm(batch)
    else:
        from tests.test_contact_change_admission import request
        request(current)
    actor = django_user_model.objects.create_user(email="reserved-ns-owner@example.test", password="testpass", is_staff=True)
    login_owner(client, actor)
    response = client.post(reverse("admin_tools:domains_edit", args=[domain.pk]), data(domain, "changed1.example.test\nchanged2.example.test"))
    assert response.status_code == 200
    assert "operation in progress" in response.content.decode()
    domain.refresh_from_db()
    assert "changed1.example.test" not in domain.cached_nameservers
