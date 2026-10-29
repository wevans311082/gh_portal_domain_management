import csv
import io
from datetime import timedelta
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.accounts.models import User
from apps.billing.models import Invoice
from apps.domains.models import Domain
from apps.support.models import SupportTicket
from apps.admin_tools.list_queries import apply_configuration, export_csv

pytestmark = pytest.mark.django_db


@pytest.fixture
def customer():
    return User.objects.create_user(email="table-customer@example.test", password="test")


def test_expiring_view_keeps_authorized_scope_and_inclusive_date_bounds(customer):
    today = timezone.localdate()
    other = User.objects.create_user(email="table-other@example.test", password="test")
    for name, owner, days in [("today.test", customer, 0), ("boundary.test", customer, 30), ("expired.test", customer, -1), ("later.test", customer, 31), ("foreign.test", other, 5)]:
        Domain.objects.create(user=owner, name=name, tld="test", expires_at=today + timedelta(days=days))
    config = {"preset": "expiring", "sort": "expires_at"}
    rows = apply_configuration(Domain.objects.filter(user=customer), "domains", config)
    assert list(rows.values_list("name", flat=True)) == ["today.test", "boundary.test"]
    response = export_csv(Domain.objects.filter(user=customer), "domains", config)
    content = response.content.decode()
    assert "today.test" in content and "boundary.test" in content
    assert "expired.test" not in content and "foreign.test" not in content


def test_overdue_view_excludes_paid_void_draft_future_and_zero_balance(customer):
    today = timezone.localdate()
    for number, status, due, paid in [("due", "unpaid", -1, 0), ("partial", "partially_paid", -2, 20), ("zero", "overdue", -3, 100), ("paid", "paid", -3, 100), ("void", "void", -3, 0), ("draft", "draft", -3, 0), ("today", "unpaid", 0, 0)]:
        Invoice.objects.create(user=customer, number=number, status=status, total=100, amount_paid=paid, due_date=today + timedelta(days=due))
    qs = apply_configuration(Invoice.objects.all(), "invoices", {"preset": "overdue", "sort": "number"})
    assert list(qs.values_list("number", flat=True)) == ["due", "partial"]


def test_open_ticket_filter_and_selected_csv_columns(customer):
    SupportTicket.objects.create(user=customer, subject="=HYPERLINK(\"bad\")", status="open")
    SupportTicket.objects.create(user=customer, subject="closed", status="closed")
    response = export_csv(SupportTicket.objects.select_related("user", "department", "assigned_to"), "tickets", {"preset": "open", "columns": ["subject", "status"]})
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows == [["Subject", "Status"], ["'=HYPERLINK(\"bad\")", "Open"]]
    assert response["Cache-Control"] == "no-store"


def test_export_refuses_over_limit_instead_of_silently_truncating(customer):
    SupportTicket.objects.bulk_create([SupportTicket(user=customer, subject=f"Ticket {i}") for i in range(1001)])
    with pytest.raises(ValidationError, match="1000"):
        export_csv(SupportTicket.objects.all(), "tickets", {})
    response = export_csv(SupportTicket.objects.select_related("user", "department", "assigned_to"), "tickets", {"filters": {"q": "Ticket 999"}})
    assert "Ticket 999" in response.content.decode()


@pytest.mark.parametrize("resource,filters", [("customers", {"active": "maybe"}), ("invoices", {"status": "made-up"}), ("tickets", {"status": "made-up"}), ("domains", {"status": "made-up"})])
def test_filter_enums_are_checked(resource, filters):
    from apps.admin_tools.list_preferences import validate_configuration
    with pytest.raises(ValidationError):
        validate_configuration(resource, {"filters": filters})
