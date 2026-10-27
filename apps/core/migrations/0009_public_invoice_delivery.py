from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0008_public_delivery_attempt_totals")]
    operations = [migrations.AlterField(
        model_name="publicsubmission", name="kind",
        field=models.CharField(max_length=16, choices=[("contact", "Contact"), ("quote", "Quote"), ("invoice", "Invoice")]),
    )]
