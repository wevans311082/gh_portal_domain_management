from django.db import migrations


CAPABILITIES = (
    ("view_customers", "View customer accounts"),
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

ROLES = {
    "Portal Support": ("view_customers", "view_domains", "view_services", "view_support", "manage_support"),
    "Portal Billing": ("view_customers", "view_billing", "manage_billing"),
    "Portal Content": ("view_content", "manage_content"),
    "Portal Domain Operations": ("view_customers", "view_domains", "manage_domains"),
    "Portal Hosting Operations": ("view_customers", "view_services", "manage_services"),
    "Portal Auditor": ("view_customers", "view_billing", "view_domains", "view_services", "view_support", "view_audit"),
}


def create_roles(apps, schema_editor):
    alias = schema_editor.connection.alias
    content_type, _ = apps.get_model("contenttypes", "ContentType").objects.using(alias).get_or_create(
        app_label="admin_tools", model="integrationsetting",
    )
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    permissions = {}
    for codename, name in CAPABILITIES:
        permissions[codename], _ = Permission.objects.using(alias).get_or_create(
            content_type=content_type, codename=codename, defaults={"name": name},
        )
    for name, capabilities in ROLES.items():
        group, _ = Group.objects.using(alias).get_or_create(name=name)
        group.permissions.add(*(permissions[code] for code in capabilities))
    # Existing staff deliberately receive no extra access automatically.


class Migration(migrations.Migration):
    dependencies = [
        ("admin_tools", "0002_integrationsetting"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [
        migrations.AlterModelOptions(
            name="integrationsetting",
            options={"ordering": ["key"], "verbose_name": "Integration setting",
                     "verbose_name_plural": "Integration settings", "permissions": CAPABILITIES},
        ),
        migrations.RunPython(create_roles, migrations.RunPython.noop),
    ]
