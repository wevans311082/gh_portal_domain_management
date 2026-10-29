import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("domains", "0026_registrar_agreements")]
    operations = [
        migrations.AddField(model_name=name, name="agreement_acceptance",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    to="domains.domainagreementacceptance"))
        for name in ("domainorder", "domaintransfer")
    ]
