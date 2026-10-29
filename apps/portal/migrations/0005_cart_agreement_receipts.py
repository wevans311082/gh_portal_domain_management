import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("domains", "0027_request_agreement_receipts"), ("portal", "0004_registrant_publication_consent")]
    operations = [migrations.AddField(model_name="portalcartitem", name="agreement_acceptance",
        field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                to="domains.domainagreementacceptance"))]
