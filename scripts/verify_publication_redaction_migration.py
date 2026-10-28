"""Exercise redaction schema/backfill against isolated historical model states."""

import json
import os
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "cyberask_domains.settings.test"
    os.environ["DJANGO_SECRET_KEY"] = "isolated-redaction-migration-check"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.utils import timezone
    assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:"
    before = [("domains", "0020_domain_contact_changes")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    user = old.get_model("accounts", "User").objects.create(email="redaction-upgrade@example.test", password="not-a-login")
    contact = old.get_model("domains", "DomainContact").objects.create(user=user, registrant_type="organisation", company="Historical Organization")
    ticket = old.get_model("support", "SupportTicket").objects.create(user=user, subject="Historical redaction case")
    Decision = old.get_model("domains", "RegistrantPublicationDecision")
    decisions = []
    for index, withdrawn, tracked in ((1, True, True), (2, False, True), (3, True, False), (4, True, True)):
        decisions.append(Decision.objects.create(customer=user, contact=contact, domain_name=f"redaction-{index}.com",
            choice="publish", subject_digest="a" * 64, policy_version="historical-policy",
            withdrawn_at=timezone.now() if withdrawn else None, withdrawal_ticket_id=ticket.pk if tracked else None))
    schema = [("domains", "0021_publication_redaction")]
    executor = MigrationExecutor(connection)
    executor.migrate(schema)
    intermediate = executor.loader.project_state(schema).apps
    intermediate.get_model("domains", "PublicationRedaction").objects.create(decision_id=decisions[0].pk, status="confirmed")
    after = [("domains", "0022_backfill_publication_redactions")]
    executor = MigrationExecutor(connection)
    executor.migrate(after)
    current = executor.loader.project_state(after).apps
    Redaction = current.get_model("domains", "PublicationRedaction")
    assert Redaction.objects.count() == 2
    assert Redaction.objects.get(decision_id=decisions[0].pk).status == "confirmed"
    assert Redaction.objects.get(decision_id=decisions[3].pk).status == "queued"
    assert not Redaction.objects.filter(decision_id__in=[decisions[1].pk, decisions[2].pk]).exists()
    assert current.get_model("domains", "RegistrantPublicationDecision").objects.filter(choice="publish").count() == 4
    print(json.dumps({"upgrade": "domains 0020 -> 0022", "tracked_withdrawals": 2,
        "existing_confirmation_preserved": True, "unsubmitted_or_active_consent_jobs": 0}))
    connection.close()


if __name__ == "__main__":
    main()
