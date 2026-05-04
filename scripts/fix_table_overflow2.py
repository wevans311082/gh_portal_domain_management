"""
Second batch: add overflow-x-auto wrapper to remaining templates with tables.
"""
import re, pathlib

BASE = pathlib.Path(__file__).parent.parent / "templates"

files = [
    "admin_tools/stats.html",
    "admin_tools/tld_pricing.html",
    "admin_tools/database.html",
    "admin_tools/task_management.html",
    "admin_tools/security.html",
    "admin_tools/resellerclub_debug.html",
    "accounts/profile.html",
    "payments/saved_cards.html",
    "domains/domain_detail.html",
    "domains/contacts/list.html",
    "dns/zone_detail.html",
    "provisioning/job_list.html",
    "provisioning/service_list.html",
    "provisioning/service_detail.html",
    "website_templates/my_templates.html",
    "support/ticket_list.html",
]

WRAPPER_OPEN = '<div class="overflow-x-auto">'
WRAPPER_CLOSE = "</div><!-- /overflow-x-auto -->"

TABLE_RE = re.compile(r"(\s*)(<table\b[^>]*>)")
END_TABLE_RE = re.compile(r"(<\/table>)")

changed = []
skipped = []

for rel in files:
    path = BASE / rel
    if not path.exists():
        print(f"MISSING: {rel}")
        continue
    text = path.read_text(encoding="utf-8")
    if "overflow-x-auto" in text:
        skipped.append(rel)
        continue
    if "<table" not in text:
        skipped.append(rel)
        continue

    # Wrap each <table>...</table> block
    out = END_TABLE_RE.sub(r"\1\n" + WRAPPER_CLOSE, text)

    def wrap_table(m):
        indent = m.group(1)
        tag = m.group(2)
        return f"{indent}{WRAPPER_OPEN}\n{indent}{tag}"

    out = TABLE_RE.sub(wrap_table, out)

    path.write_text(out, encoding="utf-8")
    changed.append(rel)

print(f"\nChanged ({len(changed)}):")
for f in changed:
    print(f"  {f}")
print(f"\nSkipped ({len(skipped)}):")
for f in skipped:
    print(f"  {f}")
