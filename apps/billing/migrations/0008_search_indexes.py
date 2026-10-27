from django.db import migrations, models
from apps.admin_tools.search_keys import SearchKey


class Migration(migrations.Migration):
    dependencies = [("billing", "0007_safe_branding_logo")]
    operations = [
        migrations.AddIndex(model_name="invoice", index=models.Index(SearchKey("number"), name="search_invoice_number")),
    ]
