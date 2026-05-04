from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("domains", "0004_domainrenewal"),
    ]

    operations = [
        migrations.AddField(
            model_name="domaincontact",
            name="company_number",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="domaincontact",
            name="registrant_validated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="domaincontact",
            name="registrant_validation_notes",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="domaincontact",
            name="registrant_validation_status",
            field=models.CharField(
                choices=[
                    ("unvalidated", "Unvalidated"),
                    ("pending", "Pending review"),
                    ("validated", "Validated"),
                    ("rejected", "Rejected"),
                ],
                default="unvalidated",
                max_length=20,
            ),
        ),
    ]
