from django.db import migrations, models
from apps.admin_tools.search_keys import SearchKey


class Migration(migrations.Migration):
    dependencies = [("billing", "0008_search_indexes")]
    operations = [
        migrations.AddIndex(model_name="quote", index=models.Index(SearchKey("number"), name="search_quote_number")),
        migrations.AddIndex(model_name="quote", index=models.Index(SearchKey("lead_email"), name="search_quote_lead_email")),
        migrations.AddIndex(model_name="quote", index=models.Index(SearchKey("lead_name"), name="search_quote_lead_name")),
    ]
