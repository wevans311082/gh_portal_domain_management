from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_tools", "0001_wizard_progress"),
    ]

    operations = [
        migrations.CreateModel(
            name="IntegrationSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("key", models.CharField(max_length=100, unique=True)),
                ("value", models.TextField(blank=True, default="")),
                ("is_secret", models.BooleanField(default=True)),
            ],
            options={
                "verbose_name": "Integration setting",
                "verbose_name_plural": "Integration settings",
                "ordering": ["key"],
            },
        ),
    ]
