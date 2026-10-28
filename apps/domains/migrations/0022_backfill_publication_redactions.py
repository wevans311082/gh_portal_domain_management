from django.db import migrations


def backfill_redactions(apps, schema_editor):
    Decision = apps.get_model("domains", "RegistrantPublicationDecision")
    Redaction = apps.get_model("domains", "PublicationRedaction")
    alias = schema_editor.connection.alias
    pending = []
    for identifier in Decision.objects.using(alias).filter(withdrawn_at__isnull=False,
            withdrawal_ticket_id__isnull=False).values_list("pk", flat=True).iterator(chunk_size=500):
        pending.append(Redaction(decision_id=identifier))
        if len(pending) == 500:
            Redaction.objects.using(alias).bulk_create(pending, batch_size=500, ignore_conflicts=True)
            pending = []
    if pending:
        Redaction.objects.using(alias).bulk_create(pending, batch_size=500, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("domains", "0021_publication_redaction")]
    operations = [migrations.RunPython(backfill_redactions, migrations.RunPython.noop)]
