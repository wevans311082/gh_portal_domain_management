from django.db import migrations, models
from apps.admin_tools.search_keys import SearchKey


class Migration(migrations.Migration):
    dependencies = [("services", "0004_service_provisioning_provider_and_config")]
    operations = [
        migrations.AddIndex(model_name="service", index=models.Index(SearchKey("domain_name"), name="search_service_domain_name")),
        migrations.AddIndex(model_name="service", index=models.Index(SearchKey("cpanel_domain"), name="search_service_cpanel_domain")),
    ]
