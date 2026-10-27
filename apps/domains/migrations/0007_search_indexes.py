from django.db import migrations, models
from apps.admin_tools.search_keys import SearchKey


class Migration(migrations.Migration):
    dependencies = [("domains", "0006_domaintransfer")]
    operations = [
        migrations.AddIndex(model_name="domain", index=models.Index(SearchKey("name"), name="search_domain_name")),
    ]
