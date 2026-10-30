"""Verify additive bulk history migration without touching a persistent database."""
import os
from pathlib import Path
import sys
from datetime import timedelta


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "cyberask_domains.settings.test"
    os.environ["DJANGO_SECRET_KEY"] = "isolated-bulk-batch-upgrade"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:"
    before = [("domains", "0027_request_agreement_receipts")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    user = old.get_model("accounts", "User").objects.create(email="bulk-upgrade@example.test", password="not-a-login")
    domain = old.get_model("domains", "Domain").objects.create(user=user, name="bulk-upgrade.com", tld="com", status="active", registrar_id="900", auto_renew=False, nameserver1="ns1.legacy.test", nameserver2="ns2.legacy.test")
    contact = old.get_model("domains", "DomainContact").objects.create(user=user, registrant_type="individual", name="Historical Customer")
    order = old.get_model("domains", "DomainOrder").objects.create(user=user, domain=domain, domain_name=domain.name, tld="com", status="completed", registration_contact=contact, admin_contact=contact, tech_contact=contact, billing_contact=contact)
    status_definition = old.get_model("domains", "DomainOrder")._meta.get_field("status").deconstruct()[3]
    executor = MigrationExecutor(connection)
    after = [("domains", "0028_bulk_domain_batches")]
    executor.migrate(after)
    current = executor.loader.project_state(after).apps
    retained = current.get_model("domains", "Domain").objects.get(pk=domain.pk)
    assert retained.registrar_id == "900" and retained.auto_renew is False
    assert current.get_model("domains", "DomainOrder").objects.get(pk=order.pk).status == "completed"
    assert current.get_model("domains", "DomainOrder")._meta.get_field("status").deconstruct()[3] == status_definition
    assert current.get_model("domains", "BulkDomainBatch").objects.count() == 0
    assert current.get_model("domains", "BulkDomainItem").objects.count() == 0
    from django.utils import timezone
    batch = current.get_model("domains", "BulkDomainBatch").objects.create(user_id=user.pk, action="lock", target={"enabled": False}, request_digest="a" * 64, manifest_digest="b" * 64, expires_at=timezone.now() + timedelta(minutes=30))
    item = current.get_model("domains", "BulkDomainItem").objects.create(batch=batch, domain_id=domain.pk, selected_id=domain.pk, name=domain.name, preview_status="ready", preview_message="Historical fixture", status="unknown")
    executor = MigrationExecutor(connection)
    after = [("domains", "0029_bulk_domain_reservations")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    assert final.get_model("domains", "BulkDomainItem").objects.get(pk=item.pk).status == "unknown"
    assert final.get_model("domains", "DomainOperationReservation").objects.count() == 0
    assert final.get_model("domains", "DomainOrder")._meta.get_field("status").deconstruct()[3] == status_definition
    executor = MigrationExecutor(connection)
    after = [("domains", "0030_bulk_provider_action")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    retained_item = final.get_model("domains", "BulkDomainItem").objects.get(pk=item.pk)
    assert retained_item.status == "unknown" and retained_item.provider_action_id == ""
    assert final.get_model("domains", "DomainOperationReservation").objects.count() == 0
    executor = MigrationExecutor(connection)
    after = [("domains", "0031_bulk_observation_time")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    retained_item = final.get_model("domains", "BulkDomainItem").objects.get(pk=item.pk)
    assert retained_item.status == "unknown" and retained_item.checked_at is None and retained_item.observed_lock is None
    executor = MigrationExecutor(connection)
    after = [("domains", "0032_complete_nameserver_cache")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    retained_domain = final.get_model("domains", "Domain").objects.get(pk=domain.pk)
    assert retained_domain.registrar_nameservers == []
    assert retained_domain.nameserver1 == "ns1.legacy.test" and retained_domain.nameserver2 == "ns2.legacy.test"
    assert final.get_model("domains", "BulkDomainItem").objects.get(pk=item.pk).status == "unknown"
    executor = MigrationExecutor(connection)
    after = [("domains", "0033_observed_nameserver_set")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    retained_item = final.get_model("domains", "BulkDomainItem").objects.get(pk=item.pk)
    assert retained_item.status == "unknown" and retained_item.observed_nameservers is None
    from apps.domains.bulk_admission import _manifest
    old_batch = final.get_model("domains", "BulkDomainBatch").objects.get(pk=batch.pk)
    old_batch.retry_of_id = None  # Duck-typed helper only; these are not old-schema columns.
    prior_manifest = _manifest(old_batch, list(old_batch.items.all()))
    final.get_model("domains", "BulkDomainBatch").objects.filter(pk=batch.pk).update(manifest_digest=prior_manifest)
    executor = MigrationExecutor(connection)
    after = [("domains", "0034_bulk_retry_lineage")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    retained_batch = final.get_model("domains", "BulkDomainBatch").objects.get(pk=batch.pk)
    assert retained_batch.retry_of_id is None and retained_batch.retry_item_ids == [] and retained_batch.retry_source_digest == ""
    assert retained_batch.manifest_digest == prior_manifest == _manifest(retained_batch, list(retained_batch.items.all()))
    executor = MigrationExecutor(connection)
    after = [("domains", "0035_bulk_dispatch_leases")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    retained_item = final.get_model("domains", "BulkDomainItem").objects.get(pk=item.pk)
    assert retained_item.status == "unknown" and retained_item.dispatch_until is None and retained_item.dispatch_token is None
    assert retained_item.dispatch_after is not None
    retained_batch = final.get_model("domains", "BulkDomainBatch").objects.get(pk=batch.pk)
    assert retained_batch.manifest_digest == prior_manifest == _manifest(retained_batch, list(retained_batch.items.all()))
    executor = MigrationExecutor(connection)
    after = [("domains", "0036_bulk_item_pause")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    retained_item = final.get_model("domains", "BulkDomainItem").objects.get(pk=item.pk)
    assert retained_item.status == "unknown" and retained_item.paused_at is None
    assert retained_item.paused_by_id is None and retained_item.pause_token is None
    executor = MigrationExecutor(connection)
    after = [("domains", "0037_nameserver_stage_outcomes")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    retained_item = final.get_model("domains", "BulkDomainItem").objects.get(pk=item.pk)
    assert retained_item.status == "unknown" and retained_item.registrar_completed_at is None and retained_item.hosting_status == ""
    executor = MigrationExecutor(connection)
    after = [("domains", "0038_dns_switch_requests")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    assert final.get_model("domains", "DomainDNSChange").objects.count() == 0
    assert final.get_model("domains", "DomainDNSReservation").objects.count() == 0
    assert final.get_model("domains", "BulkDomainItem").objects.get(pk=item.pk).status == "unknown"
    dns_change = final.get_model("domains", "DomainDNSChange").objects.create(domain_id=domain.pk, user_id=user.pk,
        requested_by_id=user.pk, target_provider="cloudflare", provider_scope="c" * 64, source={"retained": True},
        request_digest="d" * 64, manifest_digest="e" * 64, expires_at=timezone.now(), status="review",
        zone_submitted_at=timezone.now(), zone_id="retained-zone")
    executor = MigrationExecutor(connection)
    after = [("domains", "0039_dns_inventory_receipts")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    retained_dns = final.get_model("domains", "DomainDNSChange").objects.get(pk=dns_change.pk)
    assert retained_dns.status == "review" and retained_dns.zone_submitted_at is not None and retained_dns.zone_id == "retained-zone"
    assert retained_dns.inventory_receipt == {} and retained_dns.inventory_digest == "" and retained_dns.inventory_observed_at is None
    assert retained_dns.inventory_token is None and retained_dns.inventory_lease_until is None
    assert final.get_model("domains", "BulkDomainItem").objects.get(pk=item.pk).status == "unknown"
    executor = MigrationExecutor(connection)
    after = [("domains", "0040_dns_record_plans")]
    executor.migrate(after)
    final = executor.loader.project_state(after).apps
    assert final.get_model("domains", "DomainDNSRecordPlan").objects.count() == 0
    retained_dns = final.get_model("domains", "DomainDNSChange").objects.get(pk=dns_change.pk)
    assert retained_dns.approved_record_plan_id is None and retained_dns.zone_id == "retained-zone" and retained_dns.zone_submitted_at is not None
    print("Bulk/DNS upgrades passed through 0040: historical data and uncertain markers retained; no inferred inventory evidence, record plans or approvals; unrelated status choices unchanged.")
    connection.close()


if __name__ == "__main__":
    main()
