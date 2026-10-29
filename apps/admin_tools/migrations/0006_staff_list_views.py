from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("admin_tools", "0005_manual_domain_followups"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="StaffListView",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("resource", models.CharField(max_length=16)),
                ("name", models.CharField(max_length=80)),
                ("configuration", models.JSONField(default=dict)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="staff_list_views", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("name", "pk"), "constraints": [models.UniqueConstraint(fields=("owner", "resource", "name"), name="staff_list_view_owner_name")]},
        ),
    ]
