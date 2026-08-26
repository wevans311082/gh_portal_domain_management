from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse

from apps.admin_tools.config_backup import ConfigBackupError, build_backup_zip, import_backup_zip
from apps.admin_tools.models import IntegrationSetting
from apps.companies.services import CompaniesHouseService
from apps.products.models import Package
from apps.services.models import Service


def _staff(django_user_model, email="staff-mvp@example.com"):
    return django_user_model.objects.create_user(email=email, password="password123", is_staff=True)


def _user(django_user_model, email="client-mvp@example.com"):
    return django_user_model.objects.create_user(email=email, password="password123")


def _package(**kwargs):
    data = {
        "name": "plan_business",
        "display_name": "Business Hosting",
        "slug": kwargs.pop("slug", "business-hosting"),
        "price_monthly": Decimal("29.00"),
        "price_annually": Decimal("290.00"),
        "whm_package_name": "plan_business",
        "is_active": True,
    }
    data.update(kwargs)
    return Package.objects.create(**data)


@pytest.mark.django_db
def test_package_display_name_falls_back_to_name():
    pkg = _package(display_name="")
    assert pkg.get_display_name() == "plan_business"
    pkg.display_name = "Business Hosting"
    assert pkg.get_display_name() == "Business Hosting"


@pytest.mark.django_db
def test_stats_page_renders_month_axis(client, django_user_model):
    client.force_login(_staff(django_user_model))
    response = client.get(reverse("admin_tools:stats"))
    assert response.status_code == 200
    assert len(response.context["revenue_labels"]) == 12
    assert len(response.context["signup_labels"]) == 12
    assert "£" in response.content.decode() or "Revenue" in response.content.decode()


@pytest.mark.django_db
def test_settings_overview_uses_runtime_database_values(client, django_user_model, settings):
    settings.WHM_HOST = "env.example"
    IntegrationSetting.set_value("WHM_HOST", "db.example", is_secret=False)
    client.force_login(_staff(django_user_model))
    response = client.get(reverse("admin_tools:settings_overview"))
    assert response.status_code == 200
    item = response.context["cfg"]["Integrations"]["WHM_HOST"]
    assert item["value"] == "db.example"
    assert item["source"] == "database"


@pytest.mark.django_db
def test_config_backup_roundtrip(settings):
    IntegrationSetting.set_value("WHM_HOST", "backup.example", is_secret=False)
    payload = build_backup_zip("supersecret")
    IntegrationSetting.objects.all().delete()
    count = import_backup_zip(payload, "supersecret")
    assert count >= 1
    assert IntegrationSetting.get_value("WHM_HOST") == "backup.example"


@pytest.mark.django_db
def test_config_backup_rejects_bad_password():
    IntegrationSetting.set_value("WHM_HOST", "backup.example", is_secret=False)
    payload = build_backup_zip("supersecret")
    with pytest.raises(ConfigBackupError):
        import_backup_zip(payload, "wrongpass")


@pytest.mark.django_db
def test_settings_export_download(client, django_user_model):
    staff = _staff(django_user_model, "export@example.com")
    client.force_login(staff)
    response = client.post(reverse("admin_tools:settings_export"), {"backup_password": "supersecret"})
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"


@pytest.mark.django_db
def test_admin_can_set_user_password(client, django_user_model):
    staff = _staff(django_user_model, "pw-staff@example.com")
    user = _user(django_user_model, "pw-client@example.com")
    client.force_login(staff)
    with patch("apps.provisioning.whm_client.sync_user_cpanel_passwords", return_value=(0, [])):
        response = client.post(
            reverse("admin_tools:user_edit", kwargs={"pk": user.pk}),
            {
                "email": user.email,
                "first_name": "Pat",
                "last_name": "Client",
                "phone": "",
                "is_active": "on",
                "new_password1": "BrandNewPass123!",
                "new_password2": "BrandNewPass123!",
            },
        )
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.check_password("BrandNewPass123!")


@pytest.mark.django_db
def test_companies_house_sends_user_agent(monkeypatch):
    IntegrationSetting.set_value("COMPANIES_HOUSE_API_KEY", "test-key", is_secret=True)

    captured = {}

    class DummyResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"company_name": "TESCO PLC", "company_number": "00445790"}

    def fake_get(url, params=None, auth=None, headers=None, timeout=None):
        captured["headers"] = headers
        captured["auth"] = auth
        captured["url"] = url
        return DummyResponse()

    monkeypatch.setattr("apps.companies.services.requests.get", fake_get)
    payload = CompaniesHouseService().get_company("00445790")
    assert payload["company_number"] == "00445790"
    assert "User-Agent" in captured["headers"]
    assert captured["auth"][0] == "test-key"


@pytest.mark.django_db
def test_invoice_preview_page(client, django_user_model):
    client.force_login(_staff(django_user_model, "preview@example.com"))
    response = client.get(reverse("admin_tools:invoice_preview"))
    assert response.status_code == 200
    assert b"PREVIEW-0001" in response.content or b"Invoice" in response.content


@pytest.mark.django_db
def test_resellerclub_hub_page(client, django_user_model):
    staff = _staff(django_user_model, "rc@example.com")
    client.force_login(staff)
    with patch("apps.admin_tools.resellerclub_views.ResellerClubClient") as mock_cls:
        inst = MagicMock()
        inst.list_all_domain_orders.return_value = [
            {"domainname": "example.com", "orderid": "99", "currentstatus": "Active", "expiry_date": "2027-01-01"}
        ]
        inst.list_customers.return_value = []
        mock_cls.return_value = inst
        response = client.get(reverse("admin_tools:resellerclub_hub"))
    assert response.status_code == 200
    assert b"example.com" in response.content


@pytest.mark.django_db
def test_client_password_change(client, django_user_model):
    user = _user(django_user_model, "pwchange@example.com")
    client.force_login(user)
    with patch("apps.provisioning.whm_client.sync_user_cpanel_passwords", return_value=(0, [])):
        response = client.post(
            reverse("accounts_custom:profile"),
            {
                "action": "password",
                "current_password": "password123",
                "new_password1": "BrandNewPass123!",
                "new_password2": "BrandNewPass123!",
                "sync_cpanel": "on",
            },
        )
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.check_password("BrandNewPass123!")


@pytest.mark.django_db
def test_hosting_usage_and_manage_pages(client, django_user_model):
    user = _user(django_user_model, "host@example.com")
    package = _package()
    service = Service.objects.create(
        user=user,
        package=package,
        domain_name="hosted.example",
        cpanel_username="hostuser",
        status=Service.STATUS_ACTIVE,
    )
    client.force_login(user)
    with patch("apps.provisioning.whm_client.WHMClient") as mock_cls:
        inst = MagicMock()
        inst.collect_account_stats.return_value = {
            "quota": {"megabytes_used": 100, "megabytes_limit": 1000},
            "summary": {},
            "bandwidth": {"bwused": 50, "bwlimit": 1000},
            "emails": [],
            "databases": [],
            "errors": [],
        }
        mock_cls.return_value = inst
        usage = client.get(reverse("portal:hosting_usage", kwargs={"service_pk": service.pk}))
        manage = client.get(reverse("portal:hosting_manage", kwargs={"service_pk": service.pk}))
    assert usage.status_code == 200
    assert b"Business Hosting" in usage.content
    assert manage.status_code == 200
    assert b"Change package" in manage.content


@pytest.mark.django_db
def test_subscriptions_page(client, django_user_model):
    user = _user(django_user_model, "subs@example.com")
    package = _package(slug="subs-pkg")
    Service.objects.create(user=user, package=package, domain_name="subs.example", status=Service.STATUS_ACTIVE)
    client.force_login(user)
    response = client.get(reverse("portal:subscriptions"))
    assert response.status_code == 200
    assert b"Business Hosting" in response.content


@pytest.mark.django_db
def test_authenticated_domain_search_uses_portal_template(client, django_user_model):
    client.force_login(_user(django_user_model, "search@example.com"))
    response = client.get(reverse("domains:search"))
    assert response.status_code == 200
    assert b"base_portal.html" not in response.content
    assert b"Find a domain" in response.content


@pytest.mark.django_db
def test_nameservers_update_calls_resellerclub_and_whm(client, django_user_model):
    from apps.domains.models import Domain

    user = _user(django_user_model, "ns@example.com")
    package = _package(slug="ns-pkg")
    domain = Domain.objects.create(
        user=user,
        name="ns-example.com",
        tld="com",
        status=Domain.STATUS_ACTIVE,
        registrar_id="555",
    )
    Service.objects.create(
        user=user,
        package=package,
        domain_name="ns-example.com",
        cpanel_username="nsuser",
        status=Service.STATUS_ACTIVE,
    )
    client.force_login(user)
    with patch("apps.domains.views.ResellerClubClient") as rc_cls, patch(
        "apps.provisioning.whm_client.WHMClient"
    ) as whm_cls:
        rc = MagicMock()
        rc_cls.return_value = rc
        whm = MagicMock()
        whm_cls.return_value = whm
        response = client.post(
            reverse("domains:update_nameservers", kwargs={"pk": domain.pk}),
            {"nameserver1": "ns1.cyberask.co.uk", "nameserver2": "ns2.cyberask.co.uk"},
        )
    assert response.status_code == 302
    rc.modify_nameservers.assert_called_once()
    whm.modify_account_nameservers.assert_called_once()
    domain.refresh_from_db()
    assert domain.nameserver1 == "ns1.cyberask.co.uk"


@pytest.mark.django_db
def test_domain_detail_shows_inline_management_forms(client, django_user_model):
    from apps.domains.models import Domain

    user = _user(django_user_model, "detail@example.com")
    domain = Domain.objects.create(
        user=user, name="detail-example.com", tld="com", status=Domain.STATUS_ACTIVE, registrar_id="1"
    )
    client.force_login(user)
    with patch("apps.provisioning.whm_client.WHMClient") as mock_cls:
        mock_cls.return_value.get_nameservers.return_value = ["ns1.example.com", "ns2.example.com"]
        response = client.get(reverse("domains:detail", kwargs={"pk": domain.pk}))
    body = response.content.decode()
    assert response.status_code == 200
    assert "Save nameservers" in body
    assert "Get auth code" in body
    assert "Open renew wizard" in body
    assert "Open DNS editor" in body


@pytest.mark.django_db
def test_dns_zone_can_be_created(client, django_user_model):
    from apps.domains.models import Domain

    user = _user(django_user_model, "dns@example.com")
    domain = Domain.objects.create(
        user=user, name="dns-example.com", tld="com", status=Domain.STATUS_ACTIVE, registrar_id="1"
    )
    client.force_login(user)
    with patch("apps.dns.views.import_records", return_value=0):
        response = client.post(reverse("dns:zone_detail", kwargs={"domain_pk": domain.pk}), {"action": "ensure_zone"})
    assert response.status_code == 302
    domain.refresh_from_db()
    assert hasattr(domain, "dns_zone")
