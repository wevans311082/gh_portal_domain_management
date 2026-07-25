from django.db import migrations, models


def forwards(apps, schema_editor):
    Branding = apps.get_model("billing", "BillingDocumentBranding")
    for branding in Branding.objects.all():
        if branding.company_name in {"CyberAsk Domains", "CyberAsk Domains LTD", "Grumpy Hosting", "Grumpy Hosting LTD"}:
            branding.company_name = "Cyber Ask Domains"
            branding.save(update_fields=["company_name"])


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0005_alter_billingdocumentbranding_company_name"),
    ]

    operations = [
        migrations.AlterField(
            model_name="billingdocumentbranding",
            name="company_name",
            field=models.CharField(default="Cyber Ask Domains", max_length=255),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
