from django.db import migrations, models
from apps.admin_tools.search_keys import SearchKey, FullName


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_mfa_challenges")]
    operations = [
        migrations.AddIndex(model_name="user", index=models.Index(SearchKey("email"), name="search_user_email")),
        migrations.AddIndex(model_name="user", index=models.Index(SearchKey("first_name"), name="search_user_first_name")),
        migrations.AddIndex(model_name="user", index=models.Index(SearchKey("last_name"), name="search_user_last_name")),
        migrations.AddIndex(model_name="user", index=models.Index(SearchKey(FullName("first_name", "last_name")), name="search_user_full_name")),
    ]
