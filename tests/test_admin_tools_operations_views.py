import pytest
from django.urls import reverse
from django.utils import timezone

from apps.products.models import Package
from apps.services.models import Service


@pytest.mark.django_db
def test_services_list_displays_domain_and_cpanel_username(client, django_user_model):
    staff = django_user_model.objects.create_user(
        email="svc-list-admin@example.com",
        password="password123",
        is_staff=True,
    )
    user = django_user_model.objects.create_user(email="svc-owner@example.com", password="password123")
    package = Package.objects.create(
        name="Ops Hosting",
        slug="ops-hosting-opsview",
        price_monthly="10.00",
        price_annually="100.00",
        is_active=True,
    )
    Service.objects.create(
        user=user,
        package=package,
        status=Service.STATUS_ACTIVE,
        domain_name="svc-example.com",
        cpanel_username="svcacct",
    )

    client.force_login(staff)
    response = client.get(reverse("admin_tools:services_list"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "svc-example.com" in content
    assert "svcacct" in content
    assert "No push yet" in content


@pytest.mark.django_db
def test_services_edit_suspended_status_pushes_to_whm(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user(
        email="svc-edit-admin@example.com",
        password="password123",
        is_staff=True,
    )
    user = django_user_model.objects.create_user(email="svc-owner2@example.com", password="password123")
    package = Package.objects.create(
        name="Ops Hosting 2",
        slug="ops-hosting-2-opsview",
        price_monthly="10.00",
        price_annually="100.00",
        is_active=True,
    )
    service = Service.objects.create(
        user=user,
        package=package,
        status=Service.STATUS_ACTIVE,
        domain_name="svc2-example.com",
        cpanel_username="svcacct2",
        billing_period="monthly",
    )

    called = {"suspend": False}

    def fake_suspend(self, username, reason=""):
        called["suspend"] = True
        assert username == "svcacct2"
        return {"ok": True}

    monkeypatch.setattr("apps.admin_tools.operations_views.WHMClient.suspend_account", fake_suspend)

    client.force_login(staff)
    response = client.post(
        reverse("admin_tools:services_edit", kwargs={"pk": service.pk}),
        {
            "user": user.pk,
            "package": package.pk,
            "status": Service.STATUS_SUSPENDED,
            "domain_name": "svc2-example.com",
            "cpanel_username": "svcacct2",
            "cpanel_domain": "svc2-example.com",
            "cpanel_ip": "",
            "cpanel_server": "",
            "billing_period": "monthly",
            "next_due_date": "",
            "notes": "",
        },
        follow=True,
    )

    assert response.status_code == 200
    service.refresh_from_db()
    assert service.status == Service.STATUS_SUSPENDED
    assert called["suspend"] is True
    assert service.whm_last_sync_action == "suspend"
    assert service.whm_last_sync_ok is True
    assert "Suspended in WHM" in service.whm_last_sync_message
    assert service.whm_last_sync_at is not None


@pytest.mark.django_db
def test_services_edit_whm_failure_records_metadata(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user(
        email="svc-edit-fail-admin@example.com",
        password="password123",
        is_staff=True,
    )
    user = django_user_model.objects.create_user(email="svc-owner3@example.com", password="password123")
    package = Package.objects.create(
        name="Ops Hosting 3",
        slug="ops-hosting-3-opsview",
        price_monthly="10.00",
        price_annually="100.00",
        is_active=True,
    )
    service = Service.objects.create(
        user=user,
        package=package,
        status=Service.STATUS_ACTIVE,
        domain_name="svc3-example.com",
        cpanel_username="svcacct3",
        billing_period="monthly",
    )

    def fake_suspend(self, username, reason=""):
        raise Exception("WHM offline")

    monkeypatch.setattr("apps.admin_tools.operations_views.WHMClient.suspend_account", fake_suspend)

    client.force_login(staff)
    response = client.post(
        reverse("admin_tools:services_edit", kwargs={"pk": service.pk}),
        {
            "user": user.pk,
            "package": package.pk,
            "status": Service.STATUS_SUSPENDED,
            "domain_name": "svc3-example.com",
            "cpanel_username": "svcacct3",
            "cpanel_domain": "svc3-example.com",
            "cpanel_ip": "",
            "cpanel_server": "",
            "billing_period": "monthly",
            "next_due_date": "",
            "notes": "",
        },
        follow=True,
    )

    assert response.status_code == 200
    service.refresh_from_db()
    assert service.status == Service.STATUS_SUSPENDED
    assert service.whm_last_sync_ok is False
    assert "WHM offline" in service.whm_last_sync_message
    assert service.whm_last_sync_at is not None


@pytest.mark.django_db
def test_services_edit_page_shows_whm_sync_panel(client, django_user_model):
    staff = django_user_model.objects.create_user(
        email="svc-edit-panel-admin@example.com",
        password="password123",
        is_staff=True,
    )
    user = django_user_model.objects.create_user(email="svc-owner4@example.com", password="password123")
    package = Package.objects.create(
        name="Ops Hosting 4",
        slug="ops-hosting-4-opsview",
        price_monthly="10.00",
        price_annually="100.00",
        is_active=True,
    )
    service = Service.objects.create(
        user=user,
        package=package,
        status=Service.STATUS_ACTIVE,
        domain_name="svc4-example.com",
        cpanel_username="svcacct4",
        billing_period="monthly",
        whm_last_sync_action="suspend",
        whm_last_sync_at=timezone.now(),
        whm_last_sync_ok=True,
        whm_last_sync_message="Suspended in WHM successfully",
    )

    client.force_login(staff)
    response = client.get(reverse("admin_tools:services_edit", kwargs={"pk": service.pk}))

    assert response.status_code == 200
    content = response.content.decode()
    assert "WHM Sync Status" in content
    assert "Suspended in WHM successfully" in content
    assert "suspend" in content
