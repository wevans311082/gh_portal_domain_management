"""Exercise the real 0016 -> 0017 upgrade using only a fresh in-memory database."""

import json
import os
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "cyberask_domains.settings.test"
    os.environ["DJANGO_SECRET_KEY"] = "isolated-historical-migration-check"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:", "Refusing a non-isolated migration check."
    before = [("domains", "0016_registration_submission_guard"), ("audit", "0001_initial")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    User = old.get_model("accounts", "User")
    Contact = old.get_model("domains", "DomainContact")
    Order = old.get_model("domains", "DomainOrder")
    Domain = old.get_model("domains", "Domain")
    Attempt = old.get_model("domains", "DomainRegistrationAttempt")
    Audit = old.get_model("audit", "AuditLog")
    user = User.objects.create(email="historical-migration@example.test", password="not-a-login")
    contact = Contact.objects.create(user=user, label="Legacy", name="Unconfirmed person", email=user.email)
    def make(name, **values):
        return Order.objects.create(user=user, domain_name=name, tld="com", registration_contact=contact,
            admin_contact=contact, tech_contact=contact, billing_contact=contact, **values)
    draft = make("old-draft.com", status="draft", last_error="DO-NOT-COPY-OLD-PROVIDER-BODY")
    failed = make("old-failed.com", status="failed", registrar_order_id="501")
    active_domain = Domain.objects.create(user=user, name="old-complete.com", tld="com", registrar_id="502", status="active")
    completed = make(active_domain.name, status="completed", registrar_order_id="502", domain=active_domain)
    modern = make("modern-attempt.com", status="processing")
    original = Attempt.objects.create(order=modern, provider_scope="scope", customer_id="999", status="preparing")
    bootstrap = make("old-bootstrap.com", status="processing", registrar_order_id="503")
    legacy = Attempt.objects.create(order=bootstrap, provider_scope="unconfirmed", customer_id="999", status="accepted", remote_order_id="503")
    Audit.objects.create(action="domain.registration.legacy_link_retained", model_name="DomainOrder", object_id=str(bootstrap.pk))

    after = [("domains", "0017_historical_registration_review")]
    executor = MigrationExecutor(connection)
    executor.migrate(after)
    current = executor.loader.project_state(after).apps
    Attempt = current.get_model("domains", "DomainRegistrationAttempt")
    for order in (draft, failed, bootstrap):
        row = Attempt.objects.get(order_id=order.pk)
        assert row.status == "unknown" and row.legacy_review_required
        assert row.customer_id == row.provider_scope == ""
        assert "DO-NOT-COPY" not in str(row.subject)
    assert not Attempt.objects.filter(order_id=completed.pk).exists()
    row = Attempt.objects.get(pk=original.pk)
    assert row.status == "preparing" and not row.legacy_review_required
    assert Attempt.objects.get(pk=legacy.pk).legacy_review_required
    print(json.dumps({"upgrade": "0016 -> 0017", "quarantined": 3, "legacy_bootstrap_review": True, "completed_asset_preserved": True, "modern_attempt_preserved": True}))
    connection.close()


if __name__ == "__main__":
    main()
