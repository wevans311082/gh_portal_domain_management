"""Isolated additive upgrade; never infer old manual choices or enqueue work."""
import json
import os
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "cyberask_domains.settings.test"
    os.environ["DJANGO_SECRET_KEY"] = "isolated-manual-followup-upgrade"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:"
    before = [("admin_tools", "0004_customer_workspace_notes"), ("domains", "0027_request_agreement_receipts"),
        ("billing", "0010_customer_workspace_indexes"), ("services", "0006_customer_workspace_indexes"),
        ("products", "0005_package_display_name"), ("provisioning", "0003_hostingnode_websiteruntime_websitecontainer")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    user = old.get_model("accounts", "User").objects.create(email="manual-upgrade@example.test", password="not-a-login")
    contact = old.get_model("domains", "DomainContact").objects.create(user=user, registrant_type="individual", name="Historical Customer")
    order = old.get_model("domains", "DomainOrder").objects.create(user=user, domain_name="manual-upgrade.com", tld="com", status="completed",
        registration_contact=contact, admin_contact=contact, tech_contact=contact, billing_contact=contact)
    old_status = old.get_model("domains", "DomainOrder")._meta.get_field("status").deconstruct()[3]
    package = old.get_model("products", "Package").objects.create(name="Existing package", slug="existing-package", price_monthly=10, price_annually=100)
    service = old.get_model("services", "Service").objects.create(user=user, package=package, domain_name=order.domain_name)
    job = old.get_model("provisioning", "ProvisioningJob").objects.create(service=service, idempotency_key="existing-job")
    executor = MigrationExecutor(connection)
    after = [("admin_tools", "0005_manual_domain_followups")]
    executor.migrate(after)
    current = executor.loader.project_state(after).apps
    assert current.get_model("admin_tools", "ManualDomainFollowup").objects.count() == 0
    assert current.get_model("domains", "DomainOrder").objects.get(pk=order.pk).status == "completed"
    assert current.get_model("domains", "DomainOrder")._meta.get_field("status").deconstruct()[3] == old_status
    assert current.get_model("services", "Service").objects.get(pk=service.pk).domain_name == order.domain_name
    assert current.get_model("provisioning", "ProvisioningJob").objects.get(pk=job.pk).idempotency_key == "existing-job"
    print(json.dumps({"upgrade": "admin_tools 0004 -> 0005", "historical_order_service_job_preserved": True,
        "inferred_manual_followups": 0, "unrelated_status_choices_unchanged": True}))
    connection.close()


if __name__ == "__main__":
    main()
