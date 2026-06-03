import pytest
from django.urls import reverse

from apps.products.models import Package
from apps.domains.models import Domain
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


class _NestedShapeWHMClient:
    def _call(self, function: str, params: dict = None):
        if function == "version":
            return {"data": {"version": "11.122.0.1"}}
        if function == "listpkgs":
            return {
                "data": {
                    "pkg": {
                        "starter": {
                            "owner": "root",
                            "featurelist": "default",
                            "quota": "4096",
                            "bwlimit": "102400",
                        }
                    }
                }
            }
        if function == "listaccts":
            return {
                "data": {
                    "acct": [
                        {
                            "user": "acctdemo",
                            "domain": "example.com",
                            "owner": "root",
                            "plan": "starter",
                            "ip": "203.0.113.20",
                            "diskused": "700",
                            "disklimit": "4096",
                            "bandwidthused": "2048",
                            "suspended": 1,
                        }
                    ]
                }
            }
        raise AssertionError(f"Unexpected WHM function: {function}")

    def get_account_summary(self, username: str):
        return {"data": {"acct": [{"user": username, "diskusedpercent": "17"}]}}

    def get_disk_usage(self, username: str):
        return {"data": {"bandwidthused": "2048", "bandwidthlimit": "102400"}}

    def get_quota(self, username: str):
        return {"data": {"limit": "4096", "used": "700", "percent_used": "17"}}


class _ByteUsageWHMClient:
    def _call(self, function: str, params: dict = None):
        if function == "version":
            return {"version": "11.124.0.2"}
        if function == "listpkgs":
            return {"package": [{"name": "starter", "quota": "2048", "bwlimit": "10240"}]}
        if function == "listaccts":
            return {"acct": [{"user": "acctdemo", "domain": "example.com", "plan": "starter"}]}
        raise AssertionError(f"Unexpected WHM function: {function}")

    def get_account_summary(self, username: str):
        return {"data": {"acct": [{"user": username}]}}

    def get_disk_usage(self, username: str):
        # 1 GiB in bytes
        return {"data": {"totalbytes": str(1024 * 1024 * 1024)}}

    def get_quota(self, username: str):
        # 256 MiB used and 1 GiB limit in bytes
        return {"used_bytes": str(256 * 1024 * 1024), "limit_bytes": str(1024 * 1024 * 1024)}


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
def test_whm_sync_service_parses_nested_live_like_response_shapes(django_user_model):
    user = django_user_model.objects.create_user(email="nested-sync@example.com", password="password123")
    package = Package.objects.create(
        name="Starter Nested",
        slug="starter-nested",
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

    result = WHMSyncService(client=_NestedShapeWHMClient()).sync_all()

    assert result["ok"] is True
    assert WHMServerSnapshot.objects.order_by("-id").first().server_version == "11.122.0.1"
    assert WHMPackageSnapshot.objects.get(name="starter").feature_list == "default"
    account_snapshot = WHMAccountSnapshot.objects.get(username="acctdemo")
    assert account_snapshot.suspended is True
    usage_snapshot = WHMAccountUsageSnapshot.objects.get(account=account_snapshot)
    assert usage_snapshot.disk_used_mb == "700"
    assert usage_snapshot.disk_limit_mb == "4096"
    assert usage_snapshot.monthly_bandwidth_used_mb == "2048"


@pytest.mark.django_db
def test_whm_sync_service_converts_byte_usage_to_mb(django_user_model):
    user = django_user_model.objects.create_user(email="bytes-sync@example.com", password="password123")
    package = Package.objects.create(
        name="Starter Bytes",
        slug="starter-bytes",
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

    result = WHMSyncService(client=_ByteUsageWHMClient()).sync_all()

    assert result["ok"] is True
    usage_snapshot = WHMAccountUsageSnapshot.objects.get(account__username="acctdemo")
    assert usage_snapshot.disk_used_mb == "256.0"
    assert usage_snapshot.disk_limit_mb == "1024.0"
    assert usage_snapshot.monthly_bandwidth_used_mb == "1024.0"


@pytest.mark.django_db
def test_whm_reconciliation_flags_accounts_not_active_at_registrar(django_user_model):
    user = django_user_model.objects.create_user(email="reconcile@example.com", password="password123")
    package = Package.objects.create(
        name="Starter Reconcile",
        slug="starter-reconcile",
        price_monthly="9.99",
        price_annually="99.99",
    )
    service = Service.objects.create(
        user=user,
        package=package,
        domain_name="stale.example",
        cpanel_username="staleacct",
        status=Service.STATUS_ACTIVE,
    )
    WHMAccountSnapshot.objects.create(username="goodacct", domain="active.example", is_active=True)
    WHMAccountSnapshot.objects.create(username="staleacct", domain="stale.example", service=service, is_active=True)
    WHMAccountSnapshot.objects.create(username="missingacct", domain="missing.example", is_active=True)
    Domain.objects.create(user=user, name="stale.example", tld="example", status=Domain.STATUS_ACTIVE)

    report = WHMSyncService(client=_FakeWHMClient()).build_domain_reconciliation(
        registrar_orders=[
            {"domainname": "active.example", "currentstatus": "Active", "orderid": "1"},
            {"domainname": "stale.example", "currentstatus": "Deleted", "orderid": "2"},
            {"domainname": "registrar-only.example", "currentstatus": "Active", "orderid": "3"},
        ]
    )

    assert report["matched_account_total"] == 1
    assert report["orphaned_account_total"] == 2
    assert {row["account"].username for row in report["orphaned_accounts"]} == {"staleacct", "missingacct"}
    assert report["registrar_only_domain_total"] == 1
    assert report["local_stale_domain_total"] == 1


@pytest.mark.django_db
def test_terminate_orphaned_account_rechecks_registrar_before_removing(django_user_model, monkeypatch):
    user = django_user_model.objects.create_user(email="terminate-orphan@example.com", password="password123")
    package = Package.objects.create(
        name="Starter Terminate",
        slug="starter-terminate",
        price_monthly="9.99",
        price_annually="99.99",
    )
    service = Service.objects.create(
        user=user,
        package=package,
        domain_name="orphan.example",
        cpanel_username="orphanacct",
        status=Service.STATUS_ACTIVE,
    )
    WHMAccountSnapshot.objects.create(username="orphanacct", domain="orphan.example", service=service, is_active=True)

    monkeypatch.setattr(
        "apps.provisioning.whm_sync.ResellerClubClient.list_all_domain_orders",
        lambda self, no_of_records=100, status="All", include_details=False, max_details=100, max_pages=50: [],
    )

    class TerminatingClient(_FakeWHMClient):
        def __init__(self):
            self.terminated = []

        def terminate_account(self, username, keep_dns=False):
            self.terminated.append((username, keep_dns))
            return {"metadata": {"result": 1}}

    whm_client = TerminatingClient()
    WHMSyncService(client=whm_client).terminate_orphaned_account("orphanacct", keep_dns=True)

    assert whm_client.terminated == [("orphanacct", True)]
    assert WHMAccountSnapshot.objects.get(username="orphanacct").is_active is False
    service.refresh_from_db()
    assert service.status == Service.STATUS_TERMINATED
    assert service.whm_last_sync_action == "terminate_orphan"


@pytest.mark.django_db
def test_whm_integration_detail_refresh_now_runs_sync_immediately(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user(
        email="whm-admin@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

    called = {}

    monkeypatch.setattr(
        "apps.provisioning.whm_sync.WHMSyncService.sync_all",
        lambda self: called.setdefault("result", {
            "package_count": 1,
            "account_count": 2,
            "usage_count": 2,
        }),
    )

    response = client.post(
        reverse("admin_tools:integration_detail", kwargs={"service": "whm"}),
        {"action": "refresh_now"},
    )

    assert response.status_code == 302
    assert called["result"]["account_count"] == 2


@pytest.mark.django_db
def test_whm_integration_detail_reconcile_action_runs_whm_and_registrar(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user(
        email="whm-reconcile-admin@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

    called = {"synced": False, "reported": False}

    monkeypatch.setattr(
        "apps.provisioning.whm_sync.WHMSyncService.sync_all",
        lambda self: called.__setitem__("synced", True) or {"account_count": 2},
    )
    monkeypatch.setattr(
        "apps.provisioning.whm_sync.WHMSyncService.build_domain_reconciliation",
        lambda self: called.__setitem__("reported", True) or {
            "orphaned_account_total": 1,
            "registrar_only_domain_total": 0,
        },
    )

    response = client.post(
        reverse("admin_tools:integration_detail", kwargs={"service": "whm"}),
        {"action": "reconcile_domains"},
    )

    assert response.status_code == 302
    assert response.url.endswith("?reconcile=1")
    assert called == {"synced": True, "reported": True}


@pytest.mark.django_db
def test_whm_integration_detail_package_create_runs_whm_and_sync(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user(
        email="whm-pkg-admin@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

    called = {"created": False, "synced": False}

    def fake_create(self, name, options=None):
        called["created"] = True
        assert name == "starter"
        return {"ok": True}

    monkeypatch.setattr("apps.provisioning.whm_client.WHMClient.create_package", fake_create)
    monkeypatch.setattr(
        "apps.provisioning.whm_sync.WHMSyncService.sync_all",
        lambda self: called.__setitem__("synced", True) or {"package_count": 1, "account_count": 0, "usage_count": 0},
    )

    response = client.post(
        reverse("admin_tools:integration_detail", kwargs={"service": "whm"}),
        {"action": "package_create", "name": "starter", "quota": "2048", "bwlimit": "51200"},
    )

    assert response.status_code == 302
    assert called["created"] is True
    assert called["synced"] is True


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


@pytest.mark.django_db
def test_whm_integration_detail_includes_reconciliation_context(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user(
        email="whm-reconcile-view@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

    monkeypatch.setattr(
        "apps.provisioning.whm_sync.WHMSyncService.build_domain_reconciliation",
        lambda self: {
            "active_registrar_domain_total": 1,
            "whm_account_total": 1,
            "matched_account_total": 0,
            "orphaned_account_total": 1,
            "registrar_only_domain_total": 0,
            "orphaned_accounts": [],
            "registrar_only_domains": [],
            "local_stale_domains": [],
        },
    )

    response = client.get(reverse("admin_tools:integration_detail", kwargs={"service": "whm"}) + "?reconcile=1")

    assert response.status_code == 200
    assert response.context["whm_context"]["show_reconciliation"] is True
    assert response.context["whm_context"]["reconciliation"]["orphaned_account_total"] == 1


@pytest.mark.django_db
def test_resellerclub_integration_detail_includes_domain_expiry_list(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user(
        email="rc-view@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

    monkeypatch.setattr(
        "apps.domains.resellerclub_client.ResellerClubClient.list_all_domain_orders",
        lambda self, no_of_records=100, status="All", include_details=False, max_details=100, max_pages=50: [
            {
                "domainname": "example.com",
                "currentstatus": "Active",
                "orderid": 123,
                "creation_date": "2025-01-01",
                "expiry_date": "2026-01-01",
                "recurring": True,
            }
        ],
    )

    response = client.get(reverse("admin_tools:integration_detail", kwargs={"service": "resellerclub"}))

    assert response.status_code == 200
    assert response.context["resellerclub_context"]["domain_total"] == 1
    assert response.context["resellerclub_context"]["domain_orders"][0]["expiry_date"] == "2026-01-01"
    assert "managed_domain_total" in response.context["resellerclub_context"]


@pytest.mark.django_db
def test_resellerclub_refresh_now_redirects_to_full_refresh(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user(
        email="rc-refresh@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

    called = {"ok": False}

    def fake_sync(include_details=False, max_details=100):
        called["ok"] = True
        return {
            "domain_orders": [],
            "domain_total": 0,
            "synced_existing": 0,
            "created_from_service": 0,
            "unmatched_external": 0,
            "managed_domain_total": 0,
            "expiring_30d": 0,
        }

    monkeypatch.setattr("apps.admin_tools.views._sync_resellerclub_inventory", fake_sync)

    response = client.post(
        reverse("admin_tools:integration_detail", kwargs={"service": "resellerclub"}),
        {"action": "refresh_now"},
    )

    assert response.status_code == 302
    assert response.url == reverse("admin_tools:integration_detail", kwargs={"service": "resellerclub"})
    assert called["ok"] is True


@pytest.mark.django_db
def test_resellerclub_full_refresh_requests_all_fields(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user(
        email="rc-full@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff)

    captured = {}

    def fake_list(self, no_of_records=100, status="All", include_details=False, max_details=100, max_pages=50):
        captured["include_details"] = include_details
        captured["max_details"] = max_details
        captured["status"] = status
        return [
            {
                "domainname": "example.com",
                "currentstatus": "Active",
                "orderid": 123,
                "creation_date": "2025-01-01",
                "expiry_date": "2026-01-01",
                "recurring": True,
                "order_details": {"orderid": 123, "registrarlock": "true"},
            }
        ]

    monkeypatch.setattr(
        "apps.domains.resellerclub_client.ResellerClubClient.list_all_domain_orders",
        fake_list,
    )

    response = client.get(reverse("admin_tools:integration_detail", kwargs={"service": "resellerclub"}) + "?full=1")

    assert response.status_code == 200
    assert captured["include_details"] is True
    assert captured["max_details"] == 100
    assert captured["status"] == "All"
    assert response.context["resellerclub_context"]["full_refresh"] is True
    assert "registrarlock" in response.context["resellerclub_context"]["domain_orders"][0]["order_details_json"]
