"""Verify legacy consent defaults using a fresh in-memory migration upgrade."""

import os
from pathlib import Path
import sys
import json


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "cyberask_domains.settings.test"
    os.environ["DJANGO_SECRET_KEY"] = "isolated-publication-migration-check"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:", "Refusing a non-isolated check."
    before = [("domains", "0018_transfer_submission_guard"), ("portal", "0003_add_promo_code_to_cart")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    user = old.get_model("accounts", "User").objects.create(email="legacy-publication@example.test", password="not-a-login")
    contact = old.get_model("domains", "DomainContact").objects.create(user=user, registrant_type="organisation", company="Legacy Organization")
    roles = {role: contact for role in ("registration_contact", "admin_contact", "tech_contact", "billing_contact")}
    old.get_model("domains", "DomainOrder").objects.create(user=user, domain_name="legacy-org.com", tld="com", **roles)
    old.get_model("domains", "DomainTransfer").objects.create(user=user, domain_name="legacy-transfer.com", tld="com", **roles)
    cart = old.get_model("portal", "PortalCart").objects.create(user=user)
    old.get_model("portal", "PortalCartItem").objects.create(cart=cart, item_type="domain_registration", domain_name="legacy-cart.com", unit_price=12)
    after = [("domains", "0019_registrant_publication_consent"), ("portal", "0004_registrant_publication_consent")]
    executor = MigrationExecutor(connection)
    executor.migrate(after)
    current = executor.loader.project_state(after).apps
    assert not current.get_model("domains", "RegistrantPublicationDecision").objects.exists()
    for app, model in (("domains", "DomainOrder"), ("domains", "DomainTransfer"), ("portal", "PortalCartItem")):
        assert current.get_model(app, model).objects.get().publication_decision_id is None
    print(json.dumps({"upgrade": "domains 0018 -> 0019; portal 0003 -> 0004", "legacy_requests_default_redacted": 3, "inferred_consent_records": 0}))
    connection.close()


if __name__ == "__main__":
    main()
