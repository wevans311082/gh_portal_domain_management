from decimal import Decimal

from django.db import migrations, models


def create_domain_pricing_settings(apps, schema_editor):
    DomainPricingSettings = apps.get_model("domains", "DomainPricingSettings")
    if not DomainPricingSettings.objects.exists():
        DomainPricingSettings.objects.create(
            default_profit_margin_percentage=Decimal("25.00"),
            sync_enabled=True,
            sync_interval_hours=12,
            supported_tlds=["co.uk", "com", "uk", "org", "net", "io", "org.uk"],
        )


class Migration(migrations.Migration):

    dependencies = [
        ("domains", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="DomainPricingSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("default_profit_margin_percentage", models.DecimalField(decimal_places=2, default=Decimal("25.00"), max_digits=5)),
                ("sync_enabled", models.BooleanField(default=True)),
                ("sync_interval_hours", models.PositiveSmallIntegerField(default=12)),
                ("supported_tlds", models.JSONField(default=list)),
                ("last_sync_started_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_completed_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_error", models.TextField(blank=True)),
            ],
            options={
                "verbose_name": "Domain pricing settings",
                "verbose_name_plural": "Domain pricing settings",
            },
        ),
        migrations.CreateModel(
            name="TLDPricing",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("tld", models.CharField(max_length=50, unique=True)),
                ("currency", models.CharField(default="GBP", max_length=3)),
                ("registration_cost", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("renewal_cost", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("transfer_cost", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("profit_margin_percentage", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_payload", models.JSONField(blank=True, default=dict)),
            ],
            options={
                "verbose_name": "TLD pricing",
                "verbose_name_plural": "TLD pricing",
                "ordering": ["tld"],
            },
        ),
        migrations.RunPython(create_domain_pricing_settings, migrations.RunPython.noop),
    ]