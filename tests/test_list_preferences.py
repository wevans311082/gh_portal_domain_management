import pytest
from django.core.exceptions import ValidationError
from apps.accounts.models import User
from apps.admin_tools.models import StaffListView
from apps.admin_tools.list_preferences import save_view, load_view, validate_configuration

pytestmark = pytest.mark.django_db


def test_personal_views_persist_and_cannot_be_loaded_by_another_staff_member():
    owner = User.objects.create_user(email="view-owner@example.test", password="test", is_staff=True)
    other = User.objects.create_user(email="view-other@example.test", password="test", is_staff=True)
    view = save_view(actor=owner, resource="domains", name="Expiry", configuration={"columns": ["domain", "expiry"], "sort": "expires_at", "preset": "expiring", "page_size": 50})
    fresh = User.objects.get(pk=owner.pk)
    _, config = load_view(actor=fresh, resource="domains", pk=view.pk)
    assert config["columns"] == ["domain", "expiry"]
    assert config["page_size"] == 50
    with pytest.raises(StaffListView.DoesNotExist):
        load_view(actor=other, resource="domains", pk=view.pk)
    with pytest.raises(StaffListView.DoesNotExist):
        load_view(actor=owner, resource="invoices", pk=view.pk)


@pytest.mark.parametrize("config", [
    {"sort": "user__password"}, {"columns": ["password"]}, {"columns": []},
    {"columns": ["domain", "domain"]}, {"page_size": True}, {"page_size": 100000},
    {"filters": {"user__is_superuser": "true"}}, {"filters": {"q": "x" * 201}},
    {"filters": {"q": "x\x00"}}, {"preset": "unrestricted"}, {"owner": 1},
])
def test_unsupported_configuration_is_rejected(config):
    with pytest.raises(ValidationError):
        validate_configuration("domains", config)


def test_stored_corrupt_configuration_is_not_trusted():
    owner = User.objects.create_user(email="view-corrupt@example.test", password="test", is_staff=True)
    view = save_view(actor=owner, resource="tickets", name="Queue", configuration={})
    StaffListView.objects.filter(pk=view.pk).update(configuration={"sort": "user__password"})
    with pytest.raises(ValidationError):
        load_view(actor=owner, resource="tickets", pk=view.pk)


def test_saved_view_bound_and_exact_name_update():
    owner = User.objects.create_user(email="view-bound@example.test", password="test", is_staff=True)
    for i in range(20):
        save_view(actor=owner, resource="customers", name=f"View {i}", configuration={})
    with pytest.raises(ValidationError):
        save_view(actor=owner, resource="customers", name="Overflow", configuration={})
    updated = save_view(actor=owner, resource="customers", name="View 0", configuration={"page_size": 100})
    assert updated.configuration["page_size"] == 100
    assert StaffListView.objects.filter(owner=owner).count() == 20
    owner.is_staff = False
    owner.save(update_fields=["is_staff"])
    with pytest.raises(ValidationError):
        save_view(actor=owner, resource="customers", name="View 0", configuration={})
