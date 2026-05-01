from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="WizardProgress",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("completed_steps", models.JSONField(default=list)),
                ("finished", models.BooleanField(default=False)),
            ],
            options={
                "verbose_name": "Wizard Progress",
                "ordering": ["-created_at"],
                "abstract": False,
            },
        ),
    ]
