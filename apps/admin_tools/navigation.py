"""One permission-filtered navigation model for the sidebar and staff landing."""
from django.urls import reverse

from .access import has_route_access, is_owner

NAVIGATION = (
    ("Overview", (("dashboard", "Dashboard"), ("search", "Global search"), ("stats", "Stats and metrics"))),
    ("People", (("users", "Users and accounts"), ("staff_roles", "Staff roles"))),
    ("Commerce", (("invoice_list", "Invoices"), ("quote_list", "Quotes"), ("billing_branding", "Branding"), ("payments_list", "Payments"), ("promo_code_list", "Promo codes"))),
    ("Website", (("content_dashboard", "Website content overview"), ("content_settings", "Footer, legal and cookie"), ("package_card_list", "Package cards"), ("service_card_list", "Service cards"), ("faq_list", "FAQs"), ("legal_page_list", "Legal pages"), ("blog_post_list", "Blog posts"), ("testimonial_list", "Testimonials"), ("email_template_list", "Email templates"), ("banner_list", "Announcements"), ("contact_submissions_list", "Contact submissions"))),
    ("Services", (("domains_list", "Domains"), ("domain_orders_list", "Domain orders"), ("registrar_agreements", "Registrar agreements"), ("manual_domain_register", "Manual registration"), ("tld_pricing", "TLD pricing"), ("services_list", "Hosting services"), ("tickets_list", "Support tickets"), ("templates_list", "Website templates"))),
    ("System", (("task_management", "Tasks"), ("integrations_overview", "Integrations"), ("resellerclub_hub", "ResellerClub"), ("resellerclub_debug", "Registrar HTTP debug"), ("template_scan", "Import templates"), ("security", "Security and audit"), ("audit_log", "Audit log"), ("ip_allowlist", "Staff IP allowlist"), ("database", "Database"), ("settings_overview", "Settings and configuration"), ("wizard_index", "Setup wizard"))),
)


def navigation_context(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_staff:
        return {"ADMIN_NAVIGATION": [], "ADMIN_ACCESS": {}, "IS_PLATFORM_OWNER": False}
    from .access import ROUTE_POLICIES
    access = {name: policy.permits(user) for name, policy in ROUTE_POLICIES.items()}
    sections = []
    active_route = request.resolver_match.url_name if request.resolver_match else ""
    for title, entries in NAVIGATION:
        items = [{"name": name, "label": label, "url": reverse(f"admin_tools:{name}"), "active": name == active_route}
                 for name, label in entries if has_route_access(user, name)]
        if items:
            sections.append({"title": title, "items": items, "open": any(item["active"] for item in items)})
    return {"ADMIN_NAVIGATION": sections, "ADMIN_ACCESS": access, "IS_PLATFORM_OWNER": is_owner(user)}
