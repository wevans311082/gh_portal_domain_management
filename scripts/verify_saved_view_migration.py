"""Verify the additive staff preference upgrade on an isolated in-memory database."""
import os
import sys
from pathlib import Path


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "cyberask_domains.settings.test"
    os.environ["DJANGO_SECRET_KEY"] = "isolated-staff-list-upgrade"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:"
    before = [("admin_tools", "0005_manual_domain_followups")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    user = old.get_model("accounts", "User").objects.create(email="saved-view-upgrade@example.test", password="not-a-login", is_staff=True)
    note = old.get_model("admin_tools", "CustomerNote").objects.create(customer=user, author=user, body="Preserve historical note", submission_key="c69a6087-5127-4505-a457-c06f803b2e31")
    executor = MigrationExecutor(connection)
    after = [("admin_tools", "0006_staff_list_views")]
    executor.migrate(after)
    current = executor.loader.project_state(after).apps
    assert current.get_model("accounts", "User").objects.get(pk=user.pk).is_staff
    assert current.get_model("admin_tools", "CustomerNote").objects.get(pk=note.pk).body == "Preserve historical note"
    assert current.get_model("admin_tools", "StaffListView").objects.count() == 0
    print("Saved-view upgrade passed: staff and note retained; no inferred views.")
    connection.close()


if __name__ == "__main__":
    main()
