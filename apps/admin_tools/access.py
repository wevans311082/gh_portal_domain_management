"""Explicit, fail-closed authorization for every Admin Tools route."""
from dataclasses import dataclass
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.http import url_has_allowed_host_and_scheme

from apps.accounts.mfa import credential_fingerprint


CAPABILITIES = (
    ("view_customers", "View customer accounts"),
    ("manage_customer_notes", "Add internal customer notes"),
    ("view_billing", "View invoices, quotes and payments"),
    ("manage_billing", "Manage invoices, quotes and payments"),
    ("view_domains", "View domains and orders"),
    ("manage_domains", "Manage domains and orders"),
    ("view_services", "View hosting services"),
    ("manage_services", "Manage hosting services"),
    ("view_support", "View support tickets and contact enquiries"),
    ("manage_support", "Manage support tickets and contact enquiries"),
    ("view_content", "View website content"),
    ("manage_content", "Manage website content"),
    ("view_audit", "View application audit records"),
)

ROLE_CAPABILITIES = {
    "Portal Support": {"view_customers", "manage_customer_notes", "view_domains", "view_services", "view_support", "manage_support"},
    "Portal Billing": {"view_customers", "view_billing", "manage_billing"},
    "Portal Content": {"view_content", "manage_content"},
    "Portal Domain Operations": {"view_customers", "view_domains", "manage_domains"},
    "Portal Hosting Operations": {"view_customers", "view_services", "manage_services"},
    "Portal Auditor": {"view_customers", "view_billing", "view_domains", "view_services", "view_support", "view_audit"},
}


def is_owner(user):
    return bool(user.is_authenticated and user.is_active and user.is_staff and user.is_superuser)


@dataclass(frozen=True)
class RoutePolicy:
    read: str = ""
    write: str = ""
    owner: bool = False
    recent_mfa: bool = False
    staff_landing: bool = False
    read_any: tuple = ()

    def permits(self, user, method="GET"):
        if not user.is_authenticated or not user.is_active or not user.is_staff:
            return False
        if self.owner:
            return is_owner(user)
        if self.staff_landing:
            return method in {"GET", "HEAD"}
        if self.read_any and method in {"GET", "HEAD"}:
            return is_owner(user) or any(user.has_perm(f"admin_tools.{code}") for code in self.read_any)
        capability = self.read if method in {"GET", "HEAD"} else self.write
        if not capability:
            return False
        return is_owner(user) or user.has_perm(f"admin_tools.{capability}")


OWNER = RoutePolicy(owner=True, recent_mfa=True)
ROUTE_POLICIES = {"dashboard": RoutePolicy(staff_landing=True)}


def _routes(names, policy):
    for name in names.split():
        if name in ROUTE_POLICIES:
            raise ValueError(f"Duplicate route policy: {name}")
        ROUTE_POLICIES[name] = policy


_routes("search search_suggestions search_record", RoutePolicy(read_any=(
    "view_customers", "view_domains", "view_services", "view_billing", "view_support",
)))


_routes("users company_lookup", RoutePolicy("view_customers"))
_routes("customer_workspace customer_note", RoutePolicy("view_customers"))
_routes("customer_note_add", RoutePolicy(write="manage_customer_notes"))
_routes("customer_note_redact", OWNER)
_routes("identity_review", OWNER)
_routes("invoices invoice_list invoice_pdf quote_list quote_pdf payments_list", RoutePolicy("view_billing"))
_routes("invoice_create invoice_edit invoice_action quote_create quote_edit quote_action "
        "payments_create payments_edit payments_delete cart_builder cart_builder_add_hosting "
        "cart_builder_add_domain cart_builder_add_renewal cart_builder_add_transfer cart_builder_remove_item "
        "cart_builder_checkout_invoice cart_builder_checkout_quote billing_branding invoice_preview",
        RoutePolicy("manage_billing", "manage_billing"))
_routes("domains_list domain_orders_list", RoutePolicy("view_domains"))
_routes("domain_order_detail", RoutePolicy("view_domains", "manage_domains"))
_routes("domain_transfers_list domain_transfer_detail", RoutePolicy(read="view_domains"))
_routes("domains_create domains_edit domains_delete domain_order_action manual_domain_register manual_domain_result "
        "manual_domain_generate_invoice manual_domain_add_hosting tld_pricing", RoutePolicy("manage_domains", "manage_domains", recent_mfa=True))
_routes("services_list", RoutePolicy("view_services"))
_routes("services_create services_edit services_delete", RoutePolicy("manage_services", "manage_services"))
_routes("tickets_list contact_submissions_list", RoutePolicy("view_support"))
_routes("tickets_create tickets_edit tickets_delete contact_submission_detail contact_submission_delete",
        RoutePolicy("manage_support", "manage_support"))
_routes("content_dashboard package_card_list faq_list service_card_list legal_page_list error_page_list "
        "blog_post_list testimonial_list banner_list templates_list", RoutePolicy("view_content"))
_routes("content_settings package_card_edit faq_create faq_edit faq_delete service_card_create service_card_edit "
        "service_card_delete legal_page_create legal_page_edit legal_page_delete error_page_create error_page_edit "
        "error_page_delete blog_post_create blog_post_edit blog_post_delete testimonial_create testimonial_edit "
        "testimonial_delete banner_create banner_edit banner_delete templates_create templates_edit templates_delete",
        RoutePolicy("manage_content", "manage_content"))
_routes("audit_log", RoutePolicy("view_audit"))
_routes("stats user_create user_edit user_mfa_manage user_su_start staff_roles task_management template_scan "
        "integrations_overview m365_graph_config integration_detail companies_house_config resellerclub_debug "
        "resellerclub_hub security database settings_overview settings_export settings_import settings_setup_step "
        "setup wizard_index wizard_step wizard_reset email_log webhook_log feature_flags "
        "email_template_list email_template_edit email_template_delete promo_code_list promo_code_create "
        "promo_code_edit promo_code_delete contact_form_config ip_allowlist ip_allowlist_create ip_allowlist_delete", OWNER)

# Stop impersonation has its own validated owner-session restoration, because
# request.user is deliberately a customer while impersonating.
SPECIAL_ROUTES = {"user_su_stop"}


def recent_mfa(request):
    user = request.user
    if not user.is_authenticated or not user.is_active or not user.mfa_enabled:
        return False
    try:
        elapsed = timezone.now().timestamp() - float(request.session.get("_mfa_verified_at", 0))
    except (ValueError, TypeError, OverflowError):
        return False
    return (
        0 <= elapsed < settings.RECENT_MFA_SECONDS
        and request.session.get("_mfa_verified_user_id") == user.pk
        and constant_time_compare(request.session.get("_mfa_verified_hash", ""), credential_fingerprint(user))
    )


def safe_return_url(request, candidate, fallback=None):
    fallback = fallback or reverse("admin_tools:dashboard")
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate[:2048]
    return fallback


def require_recent_mfa(request):
    if recent_mfa(request):
        return None
    # Never automatically replay a POST containing credentials or a payment action.
    destination = request.get_full_path() if request.method in {"GET", "HEAD"} else request.META.get("HTTP_REFERER")
    destination = safe_return_url(request, destination)
    response = render(request, "accounts/reauthentication_required.html", {
        "mfa_enrolled": request.user.mfa_enabled,
        "reauth_url": f"{reverse('accounts_custom:reauthenticate')}?{urlencode({'next': destination})}",
        "action_was_submitted": request.method not in {"GET", "HEAD"},
    }, status=403)
    response["Cache-Control"] = "no-store"
    return response


def authorize_admin_request(request):
    match = request.resolver_match
    policy = ROUTE_POLICIES.get(match.url_name) if match and match.namespace == "admin_tools" else None
    if policy and match.url_name == "users" and request.method == "POST":
        if not is_owner(request.user):
            raise PermissionDenied("Account synchronization is owner-only.")
        return require_recent_mfa(request)
    if not policy or not policy.permits(request.user, request.method):
        raise PermissionDenied("Your role does not permit this operation.")
    if request.method not in {"GET", "HEAD"}:
        _authorize_cross_capability_action(request)
    if match.url_name == "domain_order_detail" and request.method == "POST":
        return require_recent_mfa(request)
    if policy.recent_mfa:
        return require_recent_mfa(request)


def _authorize_cross_capability_action(request):
    """Mixed legacy forms must not expand a role beyond its capabilities."""
    if is_owner(request.user):
        return
    name = request.resolver_match.url_name
    additional = set()
    if name == "manual_domain_generate_invoice" or (
        name == "manual_domain_register" and request.POST.get("create_invoice") == "on"
    ):
        additional.add("manage_billing")
    if name == "manual_domain_add_hosting" or (
        name == "manual_domain_register" and request.POST.get("create_hosting") == "on"
    ):
        additional.add("manage_services")
    if name == "domain_order_action" and request.resolver_match.kwargs.get("action") in {"mark_paid", "retry_failed"}:
        additional.add("manage_billing")
    for capability in additional:
        if not request.user.has_perm(f"admin_tools.{capability}"):
            raise PermissionDenied("This action requires an additional operational permission.")
    forbidden = {
        "package_card_edit": {"name", "whm_package_name", "price_monthly", "price_annually", "setup_fee", "provisioning_provider", "provisioning_config", "is_active"},
        "services_edit": {"user", "cpanel_username", "cpanel_domain", "cpanel_ip", "cpanel_server", "provisioning_provider", "provisioning_config"},
        "services_create": {"cpanel_username", "cpanel_domain", "cpanel_ip", "cpanel_server", "provisioning_provider", "provisioning_config"},
        "domains_edit": {"user"},
        "domain_order_detail": {"user_id", "quoted_price", "total_price", "status", "registrar_order_id"},
    }.get(name, set())
    if forbidden.intersection(request.POST):
        raise PermissionDenied("Ownership, provisioning identity and financial fields are owner-only in this form.")


def has_route_access(user, route, method="GET"):
    policy = ROUTE_POLICIES.get(route)
    return bool(policy and policy.permits(user, method))
