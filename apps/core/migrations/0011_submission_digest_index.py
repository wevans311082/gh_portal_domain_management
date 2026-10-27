from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0010_public_account_mail")]
    operations = [migrations.AlterField(
        model_name="publicsubmission", name="payload_digest",
        field=models.CharField(max_length=64, db_index=True),
    )]
