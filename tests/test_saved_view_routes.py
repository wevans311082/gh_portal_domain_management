import csv
import io
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit
import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from apps.accounts.models import User
from apps.admin_tools.models import StaffListView

pytestmark = pytest.mark.django_db


def staff_login(client, role="Portal Support"):
    user = User.objects.create_user(email="views-route@example.test", password="test", is_staff=True)
    user.groups.add(Group.objects.get(name=role))
    client.force_login(user)
    return user


def test_save_survives_logout_and_export_keeps_customer_scope(client):
    staff = staff_login(client)
    customer = User.objects.create_user(email="visible@example.test", password="test")
    response = client.post(reverse("admin_tools:saved_view_action", args=["customers"]), {"action": "save", "name": "Email only", "columns": ["email"], "page_size": "10", "sort": "email"})
    assert response.status_code == 302
    location = response["Location"]
    client.logout()
    client.force_login(staff)
    page = client.get(location)
    assert page.status_code == 200
    assert page.context["list_settings"]["columns"] == ["email"]
    exported = client.get(location + "&export=csv")
    rows = list(csv.reader(io.StringIO(exported.content.decode())))
    assert rows == [["Email"], [customer.email]]
    assert staff.email not in exported.content.decode()
    assert exported["Cache-Control"] == "no-store"


def test_dynamic_resource_permission_and_foreign_saved_view(client):
    staff = staff_login(client)
    url = reverse("admin_tools:saved_view_action", args=["invoices"])
    assert client.post(url, {"action": "save", "name": "No"}).status_code == 403
    assert not StaffListView.objects.exists()
    other = User.objects.create_user(email="foreign-view@example.test", password="test", is_staff=True)
    view = StaffListView.objects.create(owner=other, resource="customers", name="Private", configuration={})
    assert client.get(reverse("admin_tools:users"), {"view": view.pk}).status_code == 404
    assert client.post(reverse("admin_tools:saved_view_action", args=["customers"]), {"action": "delete", "view": view.pk}).status_code == 404
    assert StaffListView.objects.filter(pk=view.pk).exists()


def test_view_mutations_require_csrf_and_reset_keeps_named_views(client):
    staff = staff_login(client)
    view = StaffListView.objects.create(owner=staff, resource="customers", name="Keep", configuration={})
    url = reverse("admin_tools:saved_view_action", args=["customers"])
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(staff)
    assert csrf_client.post(url, {"action": "delete", "view": view.pk}).status_code == 403
    assert client.post(url, {"action": "reset"}).status_code == 302
    assert StaffListView.objects.filter(pk=view.pk).exists()
    assert client.post(url, {"action": "delete", "view": view.pk}).status_code == 302
    assert not StaffListView.objects.filter(pk=view.pk).exists()


@pytest.mark.parametrize("route", ["users", "domains_list", "invoice_list", "tickets_list"])
def test_invalid_list_settings_fail_closed(client, route):
    staff_login(client, "Portal Auditor")
    response = client.get(reverse("admin_tools:" + route), {"sort": "user__password", "export": "csv"})
    assert response.status_code == 400


@pytest.mark.parametrize("resource,route,columns", [
    ("customers", "users", ["name", "email"]),
    ("domains", "domains_list", ["domain", "expiry"]),
    ("invoices", "invoice_list", ["number", "due"]),
    ("tickets", "tickets_list", ["subject", "assigned"]),
])
def test_all_lists_render_selected_columns_and_applied_save_values(client, resource, route, columns):
    staff_login(client, "Portal Auditor")
    response = client.get(reverse("admin_tools:" + route), {"columns": columns, "page_size": 10})
    assert response.status_code == 200
    html = response.content.decode()
    assert re.findall(r'data-column="([^"]+)"', html) == columns
    assert 'name="page_size" value="10"' in html
    assert "Save personal view" in html and "Export filtered CSV" in html
    assert "Start customer impersonation" not in html


def test_revoked_resource_permission_cannot_reuse_view_or_export(client):
    staff = staff_login(client, "Portal Billing")
    view = StaffListView.objects.create(owner=staff, resource="invoices", name="Retained", configuration={})
    staff.groups.clear()
    assert client.get(reverse("admin_tools:invoice_list"), {"view": view.pk, "export": "csv"}).status_code == 403
    assert client.post(reverse("admin_tools:saved_view_action", args=["invoices"]), {"action": "delete", "view": view.pk}).status_code == 403
    assert StaffListView.objects.filter(pk=view.pk).exists()


def test_pagination_and_export_keep_the_applied_filter_order_and_columns(client):
    staff = staff_login(client)
    from apps.support.models import SupportTicket
    for index in range(12):
        SupportTicket.objects.create(user=staff, subject=f"Acme & Co {index:02d}")
    SupportTicket.objects.create(user=staff, subject="Excluded")
    url = reverse("admin_tools:tickets_list")
    response = client.get(url, {"q": "Acme & Co", "sort": "subject", "columns": ["subject", "status"], "page_size": 10})
    first = [obj.subject for obj in response.context["page_obj"]]
    next_url = unescape(re.search(r'rel="next" href="([^"]+)"', response.content.decode()).group(1))
    params = parse_qs(urlsplit(next_url).query)
    assert params["q"] == ["Acme & Co"] and params["columns"] == ["subject", "status"]
    second_response = client.get(url + next_url)
    second = [obj.subject for obj in second_response.context["page_obj"]]
    assert first + second == [f"Acme & Co {i:02d}" for i in range(12)]
    export_url = unescape(response.context["list_export_url"])
    exported = client.get(url + export_url)
    rows = list(csv.reader(io.StringIO(exported.content.decode())))
    assert rows[0] == ["Subject", "Status"]
    assert [row[0] for row in rows[1:]] == first + second


def test_exact_export_boundary_and_actionable_recovery(client):
    staff = staff_login(client)
    from apps.support.models import SupportTicket
    SupportTicket.objects.bulk_create([SupportTicket(user=staff, subject=f"Boundary {i:04d}") for i in range(1000)])
    url = reverse("admin_tools:tickets_list")
    params = {"q": "Boundary", "columns": ["subject"], "sort": "subject", "export": "csv"}
    response = client.get(url, params)
    assert response.status_code == 200
    assert len(list(csv.reader(io.StringIO(response.content.decode())))) == 1001
    SupportTicket.objects.create(user=staff, subject="Boundary 1000")
    held = client.get(url, params)
    assert held.status_code == 400 and "Content-Disposition" not in held
    assert "Return to table settings" in held.content.decode()
    assert "1000" in held.content.decode()
    recovery = held.context["table_return_url"]
    assert parse_qs(urlsplit(recovery).query)["q"] == ["Boundary"]
    assert "export" not in parse_qs(urlsplit(recovery).query)
    assert client.get(recovery).status_code == 200


def test_invalid_save_and_repeated_actions_do_not_change_existing_views(client):
    staff = staff_login(client)
    view = StaffListView.objects.create(owner=staff, resource="customers", name="Keep", configuration={})
    url = reverse("admin_tools:saved_view_action", args=["customers"])
    response = client.post(url, {"action": "save", "name": "", "q": "retained", "columns": ["email"]})
    assert response.status_code == 400
    assert parse_qs(urlsplit(response.context["table_return_url"]).query)["q"] == ["retained"]
    response = client.post(url, {"action": ["delete", "save"], "view": view.pk})
    assert response.status_code == 400
    view.refresh_from_db()
    assert view.name == "Keep" and view.configuration == {}


@pytest.mark.parametrize("resource,route,model_name", [("domains", "domains_list", "Domain"), ("invoices", "invoice_list", "Invoice"), ("tickets", "tickets_list", "SupportTicket")])
def test_auditor_has_read_record_navigation_without_mutation_actions(client, resource, route, model_name):
    staff = staff_login(client, "Portal Auditor")
    from apps.domains.models import Domain
    from apps.billing.models import Invoice
    from apps.support.models import SupportTicket
    obj = {"Domain": lambda: Domain.objects.create(user=staff, name="read-only.test", tld="test"), "Invoice": lambda: Invoice.objects.create(user=staff, number="READ-1"), "SupportTicket": lambda: SupportTicket.objects.create(user=staff, subject="Read-only")}[model_name]()
    response = client.get(reverse("admin_tools:" + route))
    link = reverse("admin_tools:search_record", args=[resource, obj.pk])
    html = response.content.decode()
    assert link in html and "View record" in html
    assert ">Delete</button>" not in html and ">Edit</a>" not in html
    assert client.get(link).status_code == 200
