"""Add overflow-x-auto wrappers around table elements in templates lacking them."""
import re, os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

files = [
    "templates/admin_tools/audit_log.html",
    "templates/admin_tools/contact_submissions.html",
    "templates/admin_tools/email_log.html",
    "templates/admin_tools/users.html",
    "templates/admin_tools/webhook_log.html",
    "templates/admin_tools/billing/invoice_list.html",
    "templates/admin_tools/billing/quote_list.html",
    "templates/admin_tools/content/banner_list.html",
    "templates/admin_tools/content/blog_post_list.html",
    "templates/admin_tools/content/email_template_list.html",
    "templates/admin_tools/content/error_page_list.html",
    "templates/admin_tools/content/faq_list.html",
    "templates/admin_tools/content/legal_page_list.html",
    "templates/admin_tools/content/package_card_list.html",
    "templates/admin_tools/content/promo_code_list.html",
    "templates/admin_tools/content/testimonial_list.html",
    "templates/admin_tools/ops/domains_list.html",
    "templates/admin_tools/ops/payments_list.html",
    "templates/admin_tools/ops/services_list.html",
    "templates/admin_tools/ops/templates_list.html",
    "templates/admin_tools/ops/tickets_list.html",
    "templates/admin_tools/security/ip_allowlist.html",
    "templates/dns/zone_detail.html",
    "templates/domains/my_domains.html",
    "templates/invoices/detail.html",
    "templates/invoices/list.html",
    "templates/portal/account_statement.html",
    "templates/portal/api_keys.html",
    "templates/portal/login_history.html",
    "templates/portal/my_quotes.html",
    "templates/provisioning/job_list.html",
    "templates/public/pricing.html",
    "templates/public/quote_public.html",
    "templates/support/ticket_list.html",
]

for rel in files:
    fpath = os.path.join(BASE, rel.replace("/", os.sep))
    if not os.path.exists(fpath):
        print(f"SKIP (not found): {rel}")
        continue
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    if "overflow-x-auto" in content:
        print(f"SKIP (already has overflow-x-auto): {rel}")
        continue
    # Wrap <table ...> ... </table> with an overflow-x-auto div
    new_content = re.sub(r"(<table\b)", r'<div class="overflow-x-auto">\n\1', content)
    new_content = re.sub(r"(</table>)", r"\1\n</div>", new_content)
    if new_content != content:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"FIXED: {rel}")
    else:
        print(f"NO CHANGE: {rel}")
