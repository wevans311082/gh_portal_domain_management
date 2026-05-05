import pytest
from django.urls import reverse

from apps.products.models import Package
from apps.provisioning.models import (
    WHMAccountSnapshot,
    WHMAccountUsageSnapshot,
    WHMPackageSnapshot,
    WHMServerSnapshot,
)
from apps.provisioning.whm_sync import WHMSyncService
from apps.services.models import Service


class _FakeWHMClient:
    def _call(self, function: str, params: dict = None):
        if function == "version":
            return {"version": "11.120.0.5"}
        if function == "listpkgs":
            return {
                "package": [
                    {
                        "name": "starter",
                        "owner": "root",
                        "featurelist": "default",
                        "quota": "2048",
                        "bwlimit": "51200",
                    }
                ]
            }
        if function == "listaccts":
            return {
                "acct": [
                    {
                        "user": "acctdemo",
                        "domain": "example.com",
                        "email": "owner@example.com",
                        "owner": "root",
                        "plan": "starter",
                        "ip": "203.0.113.20",
                        "suspended": 0,
                    }
                ]
            }
        raise AssertionError(f"Unexpected WHM function: {function}")

    def get_account_summary(self, username: str):
        return {"data": {"acct": [{"user": username}]}}

    def get_disk_usage(self, username: str):
        return {"bandwidthused": "1234", "bandwidthlimit": "9999"}

    def get_quota(self, username: str):
        return {
            "used": "512",
            "limit": "2048",
            "percent_used": "25",
            "inode_used": "12000",
            "inode_limit": "200000",
            "inode_percent_used": "6",
        }


@pytest.mark.django_db
def test_whm_sync_service_persists_inventory(django_user_model):
    user = django_user_model.objects.create_user(email="sync-owner@example.com", password="password123")
    package = Package.objects.create(
        name="Starter",
        slug="starter",
        price_monthly="9.99",
        price_annually="99.99",
    )
    Service.objects.create(
        user=user,
        package=package,
        domain_name="example.com",
        cpanel_username="acctdemo",
        status=Service.STATUS_ACTIVE,
    )

    result = WHMSyncService(client=_FakeWHMClient()).sync_all()

    assert result["ok"] is True
    assert result["package_count"] == 1
    assert result["account_count"] == 1
    assert WHMServerSnapshot.objects.exists()

    package_snapshot = WHMPackageSnapshot.objects.get(name="starter")
    assert package_snapshot.disk_quota_mb == "2048"

    account_snapshot = WHMAccountSnapshot.objects.get(username="acctdemo")
    assert account_snapshot.service is not None
    assert account_snapshot.service.cpanel_username == "acctdemo"

    usage_snapshot = WHMAccountUsageSnapshot.objects.get(account=account_snapshot)
    assert usage_snapshot.disk_used_mb == "512"
    assert usage_snapshot.monthly_bandwidth_used_mb == "1234"


@pytest.mark.django_db
def test_whm_integration_detail_allows_manual_sync_trigger(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user(
        email="whm-admin@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

    class _TaskResult:
        id = "task-123"

    monkeypatch.setattr(
        "apps.provisioning.tasks.sync_whm_inventory.delay",
        lambda: _TaskResult(),
    )

    response = client.post(
        reverse("admin_tools:integration_detail", kwargs={"service": "whm"}),
        {"action": "sync_now"},
    )

    assert response.status_code == 302


@pytest.mark.django_db
def test_whm_integration_detail_includes_synced_inventory_context(client, django_user_model):
    staff = django_user_model.objects.create_user(
        email="whm-view@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

    WHMServerSnapshot.objects.create(host="whm.example.com", server_version="11.120", payload={})
    WHMPackageSnapshot.objects.create(name="starter", is_active=True)
    account = WHMAccountSnapshot.objects.create(username="acctdemo", is_active=True)
    WHMAccountUsageSnapshot.objects.create(account=account, disk_used_mb="100")

    response = client.get(reverse("admin_tools:integration_detail", kwargs={"service": "whm"}))

    assert response.status_code == 200
    assert response.context["whm_context"]["package_total"] == 1
    assert response.context["whm_context"]["account_total"] == 1
    assert response.context["whm_context"]["usage_total"] == 1
