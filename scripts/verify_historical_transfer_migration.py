"""Exercise 0017 -> 0018 using a fresh in-memory database; never live data."""

import json
import os
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "cyberask_domains.settings.test"
    os.environ["DJANGO_SECRET_KEY"] = "isolated-transfer-migration-check"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:", "Refusing a non-isolated check."
    executor = MigrationExecutor(connection)
    before = [("domains", "0017_historical_registration_review")]
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    User, Contact, Transfer, Domain = (old.get_model(*key) for key in (
        ("accounts", "User"), ("domains", "DomainContact"), ("domains", "DomainTransfer"), ("domains", "Domain")))
    user = User.objects.create(email="transfer-migration@example.test", password="no-login")
    contact = Contact.objects.create(user=user, label="Legacy")
    def make(name, **values):
        return Transfer.objects.create(user=user, domain_name=name, tld="com", registration_contact=contact,
            admin_contact=contact, tech_contact=contact, billing_contact=contact,
            auth_code="PRIVATE-AUTH", last_error="PRIVATE-OLD-ERROR", **values)
    uncertain = [make(f"old-{status}.com", status=status) for status in ("pending_payment", "processing", "failed", "cancelled")]
    domain = Domain.objects.create(user=user, name="old-completed.com", tld="com", registrar_id="501", status="active")
    completed = make(domain.name, status="completed", domain=domain, registrar_order_id="501")
    after = [("domains", "0018_transfer_submission_guard")]
    executor = MigrationExecutor(connection)
    executor.migrate(after)
    current = executor.loader.project_state(after).apps
    Attempt = current.get_model("domains", "DomainTransferAttempt")
    for transfer in uncertain:
        attempt = Attempt.objects.get(transfer_id=transfer.pk)
        assert attempt.status == "unknown" and attempt.legacy_review_required
        assert "PRIVATE" not in str(attempt.subject)
    assert not Attempt.objects.filter(transfer_id=completed.pk).exists()
    print(json.dumps({"upgrade": "0017 -> 0018", "quarantined": len(uncertain), "completed_asset_preserved": True, "no_secrets_copied": True}))
    connection.close()


if __name__ == "__main__":
    main()
