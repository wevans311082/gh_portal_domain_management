"""Verify additive agreement schema without inventing historical acceptance."""

import json
import os
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "cyberask_domains.settings.test"
    os.environ["DJANGO_SECRET_KEY"] = "isolated-agreement-migration-check"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:"
    before = [("domains", "0025_eu_eligibility_declarations"), ("portal", "0004_registrant_publication_consent")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    user = old.get_model("accounts", "User").objects.create(email="agreement-upgrade@example.test", password="not-a-login")
    contact = old.get_model("domains", "DomainContact").objects.create(user=user, registrant_type="individual", name="Historical Customer")
    OldOrder = old.get_model("domains", "DomainOrder")
    order = OldOrder.objects.create(user=user, domain_name="historical-acceptance.com", tld="com", status="completed",
        registrar_order_id="501", registration_contact=contact, admin_contact=contact, tech_contact=contact, billing_contact=contact)
    old_status = OldOrder._meta.get_field("status").deconstruct()[3]
    cart = old.get_model("portal", "PortalCart").objects.create(user=user)
    item = old.get_model("portal", "PortalCartItem").objects.create(cart=cart, item_type="domain_registration",
        domain_name="historical-cart.com", description="Historical item", unit_price="12")
    after = [("domains", "0027_request_agreement_receipts"), ("portal", "0005_cart_agreement_receipts")]
    executor = MigrationExecutor(connection)
    executor.migrate(after)
    current = executor.loader.project_state(after).apps
    Order = current.get_model("domains", "DomainOrder")
    assert Order.objects.get(pk=order.pk).status == "completed"
    assert Order.objects.get(pk=order.pk).agreement_acceptance_id is None
    assert current.get_model("portal", "PortalCartItem").objects.get(pk=item.pk).agreement_acceptance_id is None
    assert Order._meta.get_field("status").deconstruct()[3] == old_status
    assert current.get_model("domains", "DomainContact").objects.get(pk=contact.pk).name == "Historical Customer"
    for model in ("RegistrarAgreementVersion", "RegistrarAgreementBinding", "RegistrarAgreementCatalogue",
                  "DomainAgreementAcceptance", "DomainAgreementAcceptanceDocument"):
        assert current.get_model("domains", model).objects.count() == 0
    print(json.dumps({"upgrade": "domains 0025 -> 0027; portal 0004 -> 0005", "historical_order_and_cart_preserved": True,
        "inferred_agreements_or_acceptance": 0, "unrelated_status_choices_unchanged": True}))
    connection.close()


if __name__ == "__main__":
    main()
