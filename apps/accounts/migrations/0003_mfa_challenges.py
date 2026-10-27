import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_mfabackupcode")]

    operations = [
        migrations.AddField(
            model_name="user", name="mfa_last_verified_step",
            field=models.BigIntegerField(default=-1, editable=False),
        ),
        migrations.AddField(
            model_name="user", name="session_version",
            field=models.UUIDField(default=uuid.uuid4, editable=False),
        ),
        migrations.CreateModel(
            name="MFALoginChallenge",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("credential_hash", models.CharField(max_length=64)),
                ("return_to", models.CharField(max_length=2048, blank=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("consumed_at", models.DateTimeField(null=True, blank=True)),
                ("user", models.ForeignKey(to="accounts.user", on_delete=django.db.models.deletion.CASCADE, related_name="mfa_challenges")),
            ],
        ),
        migrations.CreateModel(
            name="MFAAttemptWindow",
            fields=[
                ("user", models.OneToOneField(to="accounts.user", primary_key=True, serialize=False, on_delete=django.db.models.deletion.CASCADE)),
                ("started_at", models.DateTimeField()),
                ("attempts", models.PositiveIntegerField(default=0)),
            ],
        ),
    ]
