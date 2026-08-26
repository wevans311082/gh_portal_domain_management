from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("products", "0004_package_provisioning_provider_and_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="package",
            name="display_name",
            field=models.CharField(
                blank=True,
                help_text="Customer-facing name. Falls back to name when blank.",
                max_length=120,
            ),
        ),
    ]
