from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0002_service_invoice"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="whm_last_sync_action",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="service",
            name="whm_last_sync_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="service",
            name="whm_last_sync_ok",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="service",
            name="whm_last_sync_message",
            field=models.TextField(blank=True),
        ),
    ]
