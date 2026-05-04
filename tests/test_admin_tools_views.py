import pytest
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask
from django_celery_results.models import TaskResult

from apps.admin_tools.models import IntegrationSetting
from apps.billing.models import Invoice
from apps.companies.models import BusinessProfile
from apps.domains.models import DomainPricingSettings, TLDPricing
from apps.portal.models import PortalCart
from apps.products.models import Package


@pytest.mark.django_db
def test_task_management_requires_staff(client):
    response = client.get(reverse("admin_tools:task_management"))

    assert response.status_code == 302
    assert reverse("account_login") in response.url


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
    assert reverse("account_login") in response.url


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


@pytest.mark.django_db
def test_tld_pricing_page_requires_staff(client):
    response = client.get(reverse("admin_tools:tld_pricing"))

    assert response.status_code == 302
    assert reverse("account_login") in response.url


@pytest.mark.django_db
def test_staff_can_view_tld_pricing_and_loss_indicator(client, django_user_model):
    staff_user = django_user_model.objects.create_user(
        email="pricing-admin@example.com",
        password="password123",
        is_staff=True,
    )
    TLDPricing.objects.create(
        tld="com",
        registration_cost="10.00",
        renewal_cost="10.00",
        transfer_cost="10.00",
        profit_margin_percentage="-50.00",
        is_active=True,
    )

    client.force_login(staff_user)
    response = client.get(reverse("admin_tools:tld_pricing"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "TLD Pricing Management" in content
    assert ".com" in content
    assert "Sold at loss" in content


@pytest.mark.django_db
def test_resellerclub_debug_page_requires_staff(client):
    response = client.get(reverse("admin_tools:resellerclub_debug"))

    assert response.status_code == 302
    assert reverse("account_login") in response.url


@pytest.mark.django_db
def test_staff_can_view_resellerclub_debug_page(client, django_user_model):
    staff_user = django_user_model.objects.create_user(
        email="reseller-debug-admin@example.com",
        password="password123",
        is_staff=True,
    )

    client.force_login(staff_user)
    response = client.get(reverse("admin_tools:resellerclub_debug"))

    assert response.status_code == 200
    assert "ResellerClub HTTP Debug" in response.content.decode()


@pytest.mark.django_db
def test_staff_can_import_all_tlds_and_sync_inline(client, django_user_model, monkeypatch):
    staff_user = django_user_model.objects.create_user(
        email="pricing-import-admin@example.com",
        password="password123",
        is_staff=True,
    )

    class FakeClient:
        def list_available_tlds(self):
            return ["com", "net", "co.uk"]

    class FakePricingService:
        def sync_pricing(self, tlds=None, years=1):
            created = []
            for tld in (tlds or []):
                obj, _ = TLDPricing.objects.update_or_create(
                    tld=tld,
                    defaults={
                        "registration_cost": "10.00",
                        "renewal_cost": "11.00",
                        "transfer_cost": "12.00",
                        "is_active": True,
                    },
                )
                created.append(obj)
            return created

    monkeypatch.setattr("apps.domains.resellerclub_client.ResellerClubClient", lambda: FakeClient())
    monkeypatch.setattr("apps.domains.pricing.TLDPricingService", lambda: FakePricingService())
    monkeypatch.setattr(
        "apps.admin_tools.views.get_runtime_setting",
        lambda key, default="": "true" if key == "RESELLERCLUB_DEBUG_MODE" else default,
    )

    client.force_login(staff_user)
    response = client.post(reverse("admin_tools:tld_pricing"), {"action": "import_all_tlds"})

    settings_obj = DomainPricingSettings.get_solo()
    assert response.status_code == 200
    assert settings_obj.supported_tlds == ["com", "net", "co.uk"]
    assert TLDPricing.objects.filter(tld="com").exists()
    assert TLDPricing.objects.filter(tld="net").exists()
    assert TLDPricing.objects.filter(tld="co.uk").exists()


@pytest.mark.django_db
def test_staff_sync_all_runs_inline_without_celery(client, django_user_model, monkeypatch):
    staff_user = django_user_model.objects.create_user(
        email="pricing-syncall-admin@example.com",
        password="password123",
        is_staff=True,
    )
    settings_obj = DomainPricingSettings.get_solo()
    settings_obj.supported_tlds = ["com", "net"]
    settings_obj.save(update_fields=["supported_tlds"])

    class FakePricingService:
        def sync_pricing(self, tlds=None, years=1):
            created = []
            for tld in (tlds or []):
                obj, _ = TLDPricing.objects.update_or_create(
                    tld=tld,
                    defaults={
                        "registration_cost": "9.00",
                        "renewal_cost": "10.00",
                        "transfer_cost": "11.00",
                        "is_active": True,
                    },
                )
                created.append(obj)
            return created

    monkeypatch.setattr("apps.domains.pricing.TLDPricingService", lambda: FakePricingService())
    monkeypatch.setattr("apps.admin_tools.views.get_runtime_setting", lambda key, default="": default)

    client.force_login(staff_user)
    response = client.post(reverse("admin_tools:tld_pricing"), {"action": "sync_all"})

    assert response.status_code == 200
    assert TLDPricing.objects.filter(tld="com").exists()
    assert TLDPricing.objects.filter(tld="net").exists()


@pytest.mark.django_db
def test_staff_cart_builder_creates_invoice_for_selected_customer(client, django_user_model, monkeypatch):
    staff_user = django_user_model.objects.create_user(
        email="cart-builder-admin@example.com",
        password="password123",
        is_staff=True,
    )
    customer = django_user_model.objects.create_user(email="cart-customer@example.com", password="password123")
    package = Package.objects.create(
        name="Builder Hosting",
        slug="builder-hosting",
        price_monthly="12.00",
        price_annually="120.00",
        whm_package_name="builder_pkg",
    )

    monkeypatch.setattr("apps.billing.services.email_document", lambda *args, **kwargs: None)

    client.force_login(staff_user)
    response = client.post(
        reverse("admin_tools:cart_builder_add_hosting"),
        {
            "user_id": customer.pk,
            "package_id": package.pk,
            "billing_period": "monthly",
            "domain_name": "builder.example.com",
        },
    )
    assert response.status_code == 302

    response = client.post(reverse("admin_tools:cart_builder_checkout_invoice"), {"user_id": customer.pk})
    assert response.status_code == 302

    invoice = Invoice.objects.get(user=customer)
    cart = PortalCart.objects.get(invoice=invoice)
    assert invoice.created_by_staff == staff_user
    assert cart.created_by_staff == staff_user


@pytest.mark.django_db
def test_companies_house_config_requires_staff(client):
    response = client.get(reverse("admin_tools:companies_house_config"))

    assert response.status_code == 302
    assert reverse("account_login") in response.url


@pytest.mark.django_db
def test_staff_can_save_companies_house_api_key(client, django_user_model):
    staff_user = django_user_model.objects.create_user(
        email="ch-admin@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff_user)

    response = client.post(
        reverse("admin_tools:companies_house_config"),
        {
            "action": "save_key",
            "companies_house_api_key": "test-key-123",
        },
    )

    assert response.status_code == 200
    assert IntegrationSetting.get_value("COMPANIES_HOUSE_API_KEY") == "test-key-123"


@pytest.mark.django_db
def test_staff_can_open_settings_setup_step_editor(client, django_user_model):
    staff_user = django_user_model.objects.create_user(
        email="settings-editor@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff_user)

    response = client.get(
        reverse("admin_tools:settings_setup_step", kwargs={"step_key": "registrar"})
    )

    assert response.status_code == 200
    assert "Settings Editor" in response.content.decode()


@pytest.mark.django_db
def test_staff_can_save_site_settings_from_normal_admin_settings(client, django_user_model, monkeypatch):
    staff_user = django_user_model.objects.create_user(
        email="settings-save@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff_user)

    monkeypatch.setattr("apps.admin_tools.wizard_views._write_env_key", lambda *args, **kwargs: None)

    response = client.post(
        reverse("admin_tools:settings_setup_step", kwargs={"step_key": "site"}),
        {
            "action": "save",
            "site_name": "Ops Edited Name",
            "site_domain": "example.test",
            "time_zone": "UTC",
            "admin_url_slug": "manage-site-xyz/",
        },
        follow=True,
    )

    assert response.status_code == 200
    assert IntegrationSetting.get_value("SITE_NAME") == "Ops Edited Name"
    assert IntegrationSetting.get_value("SITE_DOMAIN") == "example.test"
    assert IntegrationSetting.get_value("DJANGO_TIME_ZONE") == "UTC"
    assert IntegrationSetting.get_value("DJANGO_ADMIN_URL") == "manage-site-xyz/"


@pytest.mark.django_db
def test_company_lookup_endpoint_returns_company_data(client, django_user_model, monkeypatch):
    staff_user = django_user_model.objects.create_user(
        email="lookup-admin@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff_user)

    monkeypatch.setattr(
        "apps.companies.services.CompaniesHouseService.get_company",
        lambda self, number: {
            "company_number": number,
            "company_name": "Test Company Ltd",
            "company_status": "active",
            "type": "ltd",
            "registered_office_address": {
                "address_line_1": "1 Example Street",
                "locality": "London",
                "postal_code": "SW1A 1AA",
            },
        },
    )

    response = client.get(reverse("admin_tools:company_lookup"), {"company_number": "00445790"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["company_name"] == "Test Company Ltd"
    assert payload["company_number"] == "00445790"


@pytest.mark.django_db
def test_user_create_with_company_number_creates_verified_business_profile(client, django_user_model, monkeypatch):
    staff_user = django_user_model.objects.create_user(
        email="users-admin@example.com",
        password="password123",
        is_staff=True,
    )
    client.force_login(staff_user)

    monkeypatch.setattr(
        "apps.admin_tools.forms.CompaniesHouseService.get_company",
        lambda self, number: {
            "company_number": number,
            "company_name": "Verified Widgets Ltd",
            "company_status": "active",
            "type": "ltd",
            "registered_office_address": {
                "address_line_1": "21 River Road",
                "locality": "Bristol",
                "postal_code": "BS1 4DJ",
                "country": "United Kingdom",
            },
        },
    )

    response = client.post(
        reverse("admin_tools:user_create"),
        {
            "email": "new-client@example.com",
            "first_name": "New",
            "last_name": "Client",
            "phone": "0123456789",
            "is_active": "on",
            "is_staff": "",
            "is_superuser": "",
            "password1": "StrongPass123!",
            "password2": "StrongPass123!",
            "company_name": "",
            "company_number": "00445790",
            "validate_company_with_companies_house": "on",
        },
        follow=True,
    )

    assert response.status_code == 200
    user = django_user_model.objects.get(email="new-client@example.com")
    profile = BusinessProfile.objects.get(user=user)
    assert profile.company_name == "Verified Widgets Ltd"
    assert profile.company_number == "00445790"
    assert profile.is_verified is True


@pytest.mark.django_db
def test_user_mfa_manage_requires_staff(client, django_user_model):
    target = django_user_model.objects.create_user(email="target@example.com", password="password123")
    response = client.get(reverse("admin_tools:user_mfa_manage", args=[target.pk]))

    assert response.status_code == 302
    assert reverse("account_login") in response.url


@pytest.mark.django_db
def test_staff_can_su_as_user_and_stop(client, django_user_model):
    staff = django_user_model.objects.create_user(email="staff@example.com", password="password123", is_staff=True)
    target = django_user_model.objects.create_user(email="portal-user@example.com", password="password123")
    client.force_login(staff)

    start = client.post(reverse("admin_tools:user_su_start", args=[target.pk]))
    assert start.status_code == 302
    assert start.url == reverse("portal:dashboard")

    session = client.session
    assert session.get("impersonator_user_id") == staff.pk

    stop = client.post(reverse("admin_tools:user_su_stop"))
    assert stop.status_code == 302
    assert stop.url == reverse("admin_tools:users")