from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0004_rebrand_billing_defaults"),
    ]

    operations = [
        migrations.AlterField(
            model_name="billingdocumentbranding",
            name="company_name",
            field=models.CharField(default="CyberAsk Domains", max_length=255),
        ),
    ]
