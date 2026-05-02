import pytest
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask
from django_celery_results.models import TaskResult

from apps.billing.models import Invoice


@pytest.mark.django_db
def test_task_management_requires_staff(client):
    response = client.get(reverse("admin_tools:task_management"))

    assert response.status_code == 302
    assert reverse("admin:login") in response.url


@pytest.mark.django_db
def test_staff_can_view_task_management_summary(client, django_user_model):
    staff_user = django_user_model.objects.create_user(
        email="ops@example.com",
        password="password123",
        is_staff=True,
    )
    interval = IntervalSchedule.objects.create(every=12, period=IntervalSchedule.HOURS)
    PeriodicTask.objects.create(
        name="Sync TLD pricing",
        task="apps.domains.tasks.sync_tld_pricing",
        interval=interval,
        enabled=True,
    )
    TaskResult.objects.create(
        task_id="task-1",
        task_name="apps.domains.tasks.sync_tld_pricing",
        status="SUCCESS",
        result="ok",
    )

    client.force_login(staff_user)
    response = client.get(reverse("admin_tools:task_management"))

    assert response.status_code == 200
    assert "Sync TLD pricing" in response.content.decode()
    assert response.context["enabled_periodic_tasks"] == 1


@pytest.mark.django_db
def test_staff_dashboard_shows_task_links(client, django_user_model):
    staff_user = django_user_model.objects.create_user(
        email="dashboard@example.com",
        password="password123",
        is_staff=True,
    )

    client.force_login(staff_user)
    response = client.get(reverse("admin_tools:dashboard"))

    assert response.status_code == 200
    assert reverse("admin_tools:task_management") in response.content.decode()


@pytest.mark.django_db
def test_invoices_page_requires_staff(client):
    response = client.get(reverse("admin_tools:invoices"))

    assert response.status_code == 302
    assert reverse("admin:login") in response.url


@pytest.mark.django_db
def test_staff_can_review_invoices_and_filter_by_status(client, django_user_model):
    staff_user = django_user_model.objects.create_user(
        email="invoice-admin@example.com",
        password="password123",
        is_staff=True,
    )
    user1 = django_user_model.objects.create_user(email="customer1@example.com", password="password123")
    user2 = django_user_model.objects.create_user(email="customer2@example.com", password="password123")

    Invoice.objects.create(
        user=user1,
        number="INV-AT-001",
        status=Invoice.STATUS_UNPAID,
        subtotal="120.00",
        vat_rate="0.00",
        vat_amount="0.00",
        total="120.00",
        amount_paid="0.00",
        due_date=timezone.now().date(),
    )
    Invoice.objects.create(
        user=user2,
        number="INV-AT-002",
        status=Invoice.STATUS_PAID,
        subtotal="75.00",
        vat_rate="0.00",
        vat_amount="0.00",
        total="75.00",
        amount_paid="75.00",
        due_date=timezone.now().date(),
        paid_at=timezone.now(),
    )

    client.force_login(staff_user)

    response = client.get(reverse("admin_tools:invoices"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "INV-AT-001" in content
    assert "INV-AT-002" in content

    paid_only = client.get(reverse("admin_tools:invoices"), {"status": Invoice.STATUS_PAID})
    paid_content = paid_only.content.decode()
    assert paid_only.status_code == 200
    assert "INV-AT-002" in paid_content
    assert "INV-AT-001" not in paid_content