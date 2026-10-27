from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0009_public_invoice_delivery")]
    operations = [migrations.AlterField(
        model_name="publicsubmission", name="kind",
        field=models.CharField(max_length=16, choices=[("contact", "Contact"), ("quote", "Quote"), ("invoice", "Invoice"), ("account", "Account mail")]),
    )]
