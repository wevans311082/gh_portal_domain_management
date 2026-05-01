import pytest
from django.urls import reverse
from django_celery_beat.models import IntervalSchedule, PeriodicTask
from django_celery_results.models import TaskResult


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