from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
        ("domains", "0002_domainpricingsettings_tldpricing"),
    ]

    operations = [
        migrations.CreateModel(
            name="DomainContact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("label", models.CharField(max_length=100)),
                ("name", models.CharField(max_length=255)),
                ("company", models.CharField(blank=True, max_length=255)),
                ("email", models.EmailField(max_length=254)),
                ("phone_country_code", models.CharField(default="44", max_length=8)),
                ("phone", models.CharField(max_length=32)),
                ("address_line1", models.CharField(max_length=255)),
                ("address_line2", models.CharField(blank=True, max_length=255)),
                ("city", models.CharField(max_length=100)),
                ("state", models.CharField(max_length=100)),
                ("postcode", models.CharField(max_length=20)),
                ("country", models.CharField(default="GB", max_length=2)),
                ("is_default", models.BooleanField(default=False)),
                ("registrar_contact_id", models.CharField(blank=True, max_length=255)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="domain_contacts", to="accounts.user")),
            ],
            options={
                "verbose_name": "Domain contact",
                "verbose_name_plural": "Domain contacts",
                "ordering": ["user__email", "label"],
            },
        ),
        migrations.CreateModel(
            name="DomainOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("domain_name", models.CharField(max_length=255, unique=True)),
                ("tld", models.CharField(max_length=50)),
                ("registration_years", models.PositiveSmallIntegerField(default=1)),
                ("quoted_price", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("total_price", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("pending_payment", "Pending payment"), ("paid", "Paid"), ("processing", "Processing"), ("completed", "Completed"), ("failed", "Failed"), ("cancelled", "Cancelled")], default="draft", max_length=20)),
                ("privacy_enabled", models.BooleanField(default=True)),
                ("auto_renew", models.BooleanField(default=True)),
                ("dns_provider", models.CharField(choices=[("registrar", "Registrar"), ("cpanel", "cPanel"), ("cloudflare", "Cloudflare"), ("external", "External")], default="cpanel", max_length=20)),
                ("registrar_order_id", models.CharField(blank=True, max_length=255)),
                ("last_error", models.TextField(blank=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("admin_contact", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="admin_orders", to="domains.domaincontact")),
                ("billing_contact", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="billing_orders", to="domains.domaincontact")),
                ("domain", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="order", to="domains.domain")),
                ("invoice", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="domain_orders", to="billing.invoice")),
                ("registration_contact", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="registration_orders", to="domains.domaincontact")),
                ("tech_contact", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="tech_orders", to="domains.domaincontact")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="domain_orders", to="accounts.user")),
            ],
            options={
                "verbose_name": "Domain order",
                "verbose_name_plural": "Domain orders",
                "ordering": ["-created_at"],
            },
        ),
    ]
