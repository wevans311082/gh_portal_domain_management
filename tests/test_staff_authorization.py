"""SEC-02: route inventory, least privilege, recent MFA, roles and impersonation."""
from datetime import timedelta
from urllib.parse import urlparse

import pyotp
import pytest
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import MFAAttemptWindow
from apps.accounts.mfa import regenerate_backup_codes
from apps.admin_tools.access import CAPABILITIES, ROLE_CAPABILITIES, ROUTE_POLICIES, SPECIAL_ROUTES
from apps.admin_tools.impersonation import CONTEXT_KEY
from apps.admin_tools.models import IntegrationSetting
from apps.admin_tools.urls import urlpatterns
from apps.audit.models import AuditLog
from apps.products.models import Package
from tests.security_helpers import login_owner

pytestmark = pytest.mark.django_db


def route_url(name):
    pattern = next(pattern for pattern in urlpatterns if pattern.name == name)
    kwargs = {key: 1 if converter.__class__.__name__ == "IntConverter" else "test"
              for key, converter in pattern.pattern.converters.items()}
    return reverse(f"admin_tools:{name}", kwargs=kwargs)


@pytest.fixture(autouse=True)
def clear_throttles():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def staff(django_user_model):
    return django_user_model.objects.create_user(email="staff-access@example.test", password="staff-password", is_staff=True)


@pytest.fixture
def owner(client, django_user_model):
    user = django_user_model.objects.create_user(email="owner-access@example.test", password="initial")
    login_owner(client, user)
    return user


def grant(user, role):
    user.groups.add(Group.objects.get(name=role))


def test_every_admin_route_has_an_explicit_policy_and_guard():
    from apps.admin_tools.checks import check_route_policies
    assert not check_route_policies(None)
    assert {pattern.name for pattern in urlpatterns} == set(ROUTE_POLICIES) | SPECIAL_ROUTES
    for pattern in urlpatterns:
        if pattern.name not in SPECIAL_ROUTES:
            assert hasattr(pattern.callback, "__wrapped__"), pattern.name


@pytest.mark.parametrize("name", sorted(name for name in ROUTE_POLICIES if name != "dashboard"))
@pytest.mark.parametrize("method", ["get", "post"])
def test_unassigned_staff_cannot_access_any_operational_or_owner_route(client, staff, name, method):
    client.force_login(staff)
    before = IntegrationSetting.objects.count()
    response = getattr(client, method)(route_url(name), {})
    assert response.status_code == 403, (name, response.status_code)
    assert IntegrationSetting.objects.count() == before
    staff.refresh_from_db()
    assert not staff.is_superuser


@pytest.mark.parametrize("role", sorted(ROLE_CAPABILITIES))
def test_predefined_roles_match_the_documented_least_privilege_set(role):
    group = Group.objects.get(name=role)
    assert set(group.permissions.values_list("codename", flat=True)) == ROLE_CAPABILITIES[role]


@pytest.mark.parametrize("role,allowed,denied", [
    ("Portal Support", "tickets_list", "invoice_list"),
    ("Portal Billing", "invoice_list", "services_list"),
    ("Portal Content", "faq_list", "users"),
    ("Portal Domain Operations", "domains_list", "payments_list"),
    ("Portal Hosting Operations", "services_list", "invoice_list"),
    ("Portal Auditor", "audit_log", "invoice_create"),
])
def test_roles_allow_their_work_and_deny_other_work(client, staff, role, allowed, denied):
    grant(staff, role)
    client.force_login(staff)
    assert client.get(route_url(allowed)).status_code == 200
    assert client.get(route_url(denied)).status_code == 403


@pytest.mark.parametrize("role", sorted(ROLE_CAPABILITIES))
def test_no_operational_role_can_escalate_or_export_secrets(client, staff, role):
    grant(staff, role)
    # Even all low-level Django permissions must not bypass owner-only policies.
    staff.user_permissions.add(*Permission.objects.filter(content_type__app_label="accounts"))
    client.force_login(staff)
    assert client.post(reverse("admin_tools:user_edit", args=[staff.pk]), {"is_superuser": "on"}).status_code == 403
    assert client.post(reverse("admin_tools:settings_export"), {"backup_password": "a-long-password"}).status_code == 403
    assert client.get(reverse("admin:index")).status_code == 403
    staff.refresh_from_db()
    assert not staff.is_superuser


def test_staff_dashboard_and_navigation_do_not_leak_owner_or_billing_data(client, staff, django_user_model):
    grant(staff, "Portal Support")
    django_user_model.objects.create_superuser(email="hidden-owner@example.test", password="owner-password")
    client.force_login(staff)
    response = client.get(reverse("admin_tools:dashboard"))
    html = response.content.decode()
    assert "Your workspace" in html and "Support tickets" in html
    assert "Revenue (30d)" not in html
    assert "Settings and configuration" not in html and "Django Admin" not in html
    response = client.get(reverse("admin_tools:users"))
    assert b"hidden-owner@example.test" not in response.content
    assert b"Edit user" not in response.content and b"Sync WHM" not in response.content


def test_auditor_cannot_mutate_support_or_financial_records(client, staff):
    grant(staff, "Portal Auditor")
    client.force_login(staff)
    assert client.post(reverse("admin_tools:tickets_create"), {"subject": "forged"}).status_code == 403
    assert client.post(reverse("admin_tools:payments_create"), {}).status_code == 403


def test_expired_owner_mfa_blocks_side_effects_and_explains_retry(client, owner):
    session = client.session
    session["_mfa_verified_at"] = (timezone.now() - timedelta(seconds=301)).timestamp()
    session.save()
    response = client.post(reverse("admin_tools:settings_import"), {"backup_password": "secret"})
    assert response.status_code == 403
    assert b"Your action has not been performed" in response.content
    assert "no-store" in response["Cache-Control"]
    assert IntegrationSetting.objects.count() == 0


def test_owner_without_mfa_is_directed_to_enrollment(client, django_user_model):
    user = django_user_model.objects.create_superuser(email="unenrolled@example.test", password="password")
    client.force_login(user)
    response = client.get(reverse("admin_tools:staff_roles"))
    assert response.status_code == 403
    assert b"Set up MFA" in response.content


def test_password_and_new_factor_restore_recent_authorization(client, owner):
    session = client.session
    session["_mfa_verified_at"] = 0
    session.save()
    backup = regenerate_backup_codes(owner, count=1)[0]
    response = client.post(reverse("accounts_custom:reauthenticate"), {
        "password": "test-owner-authentication-password!", "code": backup, "next": reverse("admin_tools:staff_roles"),
    })
    assert response.url == reverse("admin_tools:staff_roles")
    assert client.get(response.url).status_code == 200
    assert client.post(reverse("accounts_custom:reauthenticate"), {"password": "test-owner-authentication-password!", "code": backup}).status_code == 200


def test_wrong_password_attempts_are_throttled_without_consuming_factor(client, owner):
    from apps.accounts.mfa import regenerate_backup_codes, consume_backup_code
    backup = regenerate_backup_codes(owner, count=1)[0]
    for _ in range(settings.MFA_MAX_ATTEMPTS):
        assert client.post(reverse("accounts_custom:reauthenticate"), {"password": "wrong", "code": backup}).status_code == 200
    assert client.post(reverse("accounts_custom:reauthenticate"), {"password": "wrong", "code": backup}).status_code == 429
    MFAAttemptWindow.objects.filter(user=owner).update(started_at=timezone.now() - timedelta(seconds=301))
    assert consume_backup_code(owner, backup)


def test_role_assignment_is_audited_and_revokes_existing_sessions(client, owner, staff):
    other = Client()
    other.force_login(staff)
    response = client.post(reverse("admin_tools:staff_roles"), {
        "user": staff.pk, "roles": ["Portal Support"], "reason": "Assign customer support duties",
    })
    assert response.status_code == 302
    assert set(staff.groups.values_list("name", flat=True)) == {"Portal Support"}
    assert AuditLog.objects.filter(action="staff.roles.changed", object_id=str(staff.pk)).exists()
    assert other.get(reverse("admin_tools:dashboard")).status_code == 302
    response = client.post(reverse("admin_tools:staff_roles"), {"user": staff.pk, "roles": [], "reason": "Remove operational duties"})
    assert response.status_code == 302
    assert not staff.groups.exists()


def test_owner_cannot_change_own_flags_through_user_editor(client, owner):
    response = client.post(reverse("admin_tools:user_edit", args=[owner.pk]), {
        "email": owner.email, "is_active": "on", "is_staff": "on", "first_name": "Owner",
    })
    assert response.status_code == 200
    owner.refresh_from_db()
    assert owner.is_superuser
    assert b"cannot change your own" in response.content


def test_owner_cannot_disable_own_mfa_through_admin_reset(client, owner):
    assert client.post(reverse("admin_tools:user_mfa_manage", args=[owner.pk]), {"action": "disable_mfa"}).status_code == 403
    owner.refresh_from_db()
    assert owner.mfa_enabled


def test_impersonation_is_reasoned_audited_and_reversible(client, owner, django_user_model):
    target = django_user_model.objects.create_user(email="customer-impersonation@example.test", password="customer")
    response = client.post(reverse("admin_tools:user_su_start", args=[target.pk]), {"reason": "Investigate support ticket 123"})
    assert response.url == reverse("portal:dashboard")
    assert client.session["_auth_user_id"] == str(target.pk)
    assert AuditLog.objects.filter(user=owner, action="impersonation.started", object_id=str(target.pk)).exists()
    assert client.post(reverse("accounts_custom:mfa_disable"), {"token": "123456"}).status_code == 403
    assert client.get(reverse("portal:api_key_create")).status_code == 403
    response = client.post(reverse("admin_tools:user_su_stop"))
    assert response.url == reverse("admin_tools:users")
    assert client.session["_auth_user_id"] == str(owner.pk)
    assert "_mfa_verified_at" not in client.session
    assert AuditLog.objects.filter(user=owner, action="impersonation.stopped").exists()


@pytest.mark.parametrize("kind", ["staff", "owner", "inactive"])
def test_privileged_or_inactive_targets_cannot_be_impersonated(client, owner, django_user_model, kind):
    target = django_user_model.objects.create_user(email="target@example.test", password="password",
        is_staff=kind == "staff", is_superuser=kind == "owner", is_active=kind != "inactive")
    assert client.post(reverse("admin_tools:user_su_start", args=[target.pk]), {"reason": "Support"}).status_code == 403
    assert client.session["_auth_user_id"] == str(owner.pk)


@pytest.mark.parametrize("change", ["expired", "password", "demoted"])
def test_impersonation_is_revoked_when_owner_authority_changes(client, owner, django_user_model, change):
    target = django_user_model.objects.create_user(email="customer@example.test", password="password")
    client.post(reverse("admin_tools:user_su_start", args=[target.pk]), {"reason": "Support"})
    if change == "expired":
        session = client.session
        context = session[CONTEXT_KEY]
        context["started_at"] = timezone.now().timestamp() - settings.IMPERSONATION_LIFETIME_SECONDS - 1
        session[CONTEXT_KEY] = context
        session.save()
    elif change == "password":
        owner.set_password("different")
        owner.save(update_fields=["password"])
    else:
        owner.is_superuser = False
        owner.save(update_fields=["is_superuser"])
    assert client.get(reverse("portal:dashboard")).status_code == 403
    assert "_auth_user_id" not in client.session


def test_content_editor_cannot_change_provisioning_or_prices(client, staff):
    grant(staff, "Portal Content")
    client.force_login(staff)
    package = Package.objects.create(name="Plan", slug="security-plan", price_monthly="10", price_annually="100")
    response = client.get(reverse("admin_tools:package_card_edit", args=[package.pk]))
    assert response.status_code == 200
    assert b'name="provisioning_config"' not in response.content
    assert client.post(reverse("admin_tools:package_card_edit", args=[package.pk]), {"price_monthly": "0"}).status_code == 403
    package.refresh_from_db()
    assert str(package.price_monthly) == "10.00"


def test_reauthentication_does_not_redirect_off_site(client, owner):
    from apps.accounts.mfa import regenerate_backup_codes
    code = regenerate_backup_codes(owner, count=1)[0]
    response = client.post(reverse("accounts_custom:reauthenticate"), {"password": "test-owner-authentication-password!", "code": code, "next": "https://evil.example"})
    assert not urlparse(response.url).netloc


@pytest.mark.parametrize("field,value", [
    ("_mfa_verified_at", "invalid"), ("_mfa_verified_at", float("inf")),
    ("_mfa_verified_at", 99999999999), ("_mfa_verified_user_id", -1),
    ("_mfa_verified_hash", "forged"),
])
def test_recent_authorization_is_bound_to_user_credentials_and_time(client, owner, field, value):
    session = client.session
    session[field] = value
    session.save()
    assert client.get(reverse("admin_tools:staff_roles")).status_code == 403
    assert client.get(reverse("admin:index")).status_code == 403


@pytest.mark.parametrize("name,data,kwargs", [
    ("manual_domain_generate_invoice", {}, {"order_id": 1}),
    ("manual_domain_add_hosting", {}, {"order_id": 1}),
    ("manual_domain_register", {"create_invoice": "on"}, {}),
    ("manual_domain_register", {"create_hosting": "on"}, {}),
    ("domain_order_action", {}, {"pk": 1, "action": "mark_paid"}),
    ("domain_order_action", {}, {"pk": 1, "action": "retry_failed"}),
])
def test_domain_role_cannot_cross_into_billing_or_hosting(client, staff, name, data, kwargs):
    grant(staff, "Portal Domain Operations")
    client.force_login(staff)
    # Denial must precede looking up even a nonexistent object or provider work.
    assert client.post(reverse(f"admin_tools:{name}", kwargs=kwargs), data).status_code == 403


def test_permission_revocation_is_effective_on_the_next_request(client, staff):
    grant(staff, "Portal Support")
    client.force_login(staff)
    assert client.get(reverse("admin_tools:tickets_list")).status_code == 200
    staff.groups.clear()
    assert client.get(reverse("admin_tools:tickets_list")).status_code == 403


@pytest.mark.parametrize("roles", [["Portal Administrator"], ["Portal Support", "unknown"]])
def test_unknown_roles_are_rejected_without_assignments(client, owner, staff, roles):
    response = client.post(reverse("admin_tools:staff_roles"), {
        "user": staff.pk, "roles": roles, "reason": "Invalid assignment",
    })
    assert response.status_code == 200
    assert not staff.groups.exists()
    assert not AuditLog.objects.filter(action="staff.roles.changed").exists()


def test_role_assignment_cannot_target_an_owner(client, owner):
    response = client.post(reverse("admin_tools:staff_roles"), {
        "user": owner.pk, "roles": ["Portal Support"], "reason": "Attempt self assignment",
    })
    assert response.status_code == 200
    assert not owner.groups.exists()
    assert not AuditLog.objects.filter(action="staff.roles.changed").exists()


def test_sensitive_owner_posts_require_csrf(client, owner, staff):
    strict = Client(enforce_csrf_checks=True)
    strict.cookies = client.cookies
    response = strict.post(reverse("admin_tools:staff_roles"), {
        "user": staff.pk, "roles": ["Portal Support"], "reason": "Missing CSRF token",
    })
    assert response.status_code == 403
    assert not staff.groups.exists()


def test_failed_mandatory_audit_does_not_start_impersonation(client, owner, django_user_model, monkeypatch):
    target = django_user_model.objects.create_user(email="audit-target@example.test", password="password")
    def fail_audit(*args, **kwargs):
        raise RuntimeError("Audit unavailable")
    monkeypatch.setattr(AuditLog.objects, "create", fail_audit)
    with pytest.raises(RuntimeError, match="Audit unavailable"):
        client.post(reverse("admin_tools:user_su_start", args=[target.pk]), {"reason": "Investigate"})
    assert client.session["_auth_user_id"] == str(owner.pk)
    assert CONTEXT_KEY not in client.session


def test_sensitive_admin_responses_are_not_cacheable(client, owner):
    for name in ("staff_roles", "settings_overview"):
        response = client.get(reverse(f"admin_tools:{name}"))
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]


@pytest.mark.parametrize("name", ["account_change_password", "account_set_password", "account_email", "account_reset_password"])
def test_alternate_account_management_is_blocked_during_impersonation(client, owner, django_user_model, name):
    target = django_user_model.objects.create_user(email="alternate-target@example.test", password="password")
    client.post(reverse("admin_tools:user_su_start", args=[target.pk]), {"reason": "Support"})
    assert client.get(reverse(name)).status_code == 403
    assert client.post(reverse(name), {"email": "attacker@example.test", "password1": "a-new-password", "password2": "a-new-password"}).status_code == 403
    target.refresh_from_db()
    assert target.email == "alternate-target@example.test"
    assert target.check_password("password")
