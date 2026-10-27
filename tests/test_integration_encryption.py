"""SEC-05: raw storage, authentication, rotation and fail-closed consumers."""
import base64
import io

import pytest
from django.core.cache import cache
from django.core.management import call_command, CommandError
from django.db import connection, OperationalError
from django.test import override_settings
from django.urls import reverse

from apps.admin_tools.models import IntegrationSetting
from apps.admin_tools.secrets import encrypt_value, decrypt_value, IntegrationSecretError, PREFIX
from apps.admin_tools.config_backup import build_backup_zip, import_backup_zip
from apps.core.runtime_settings import get_runtime_setting, get_runtime_setting_with_source
from tests.security_helpers import login_owner

pytestmark = pytest.mark.django_db
OLD_KEY = base64.b64encode(b"a" * 32).decode()
NEW_KEY = base64.b64encode(b"b" * 32).decode()


def raw_value(pk, value=None):
    # Only tests/controlled migration tooling bypass the guarded ORM writers.
    with connection.cursor() as cursor:
        if value is not None:
            cursor.execute("UPDATE admin_tools_integrationsetting SET value=%s WHERE id=%s", [value, pk])
        cursor.execute("SELECT value FROM admin_tools_integrationsetting WHERE id=%s", [pk])
        return cursor.fetchone()[0]


@pytest.mark.parametrize("is_secret", [True, False])
def test_database_contains_only_authenticated_ciphertext_even_when_flag_is_wrong(is_secret):
    IntegrationSetting.set_value("M365_GRAPH_CLIENT_SECRET", "secret-not-in-database", is_secret=is_secret)
    row = IntegrationSetting.objects.get(key="M365_GRAPH_CLIENT_SECRET")
    stored = raw_value(row.pk)
    assert stored.startswith(PREFIX)
    assert "secret-not-in-database" not in stored
    assert row.plaintext_value == "secret-not-in-database"
    assert get_runtime_setting(row.key) == "secret-not-in-database"


def test_encryption_uses_fresh_nonces_and_is_bound_to_key_name():
    first = encrypt_value("WHM_API_TOKEN", "same-secret")
    second = encrypt_value("WHM_API_TOKEN", "same-secret")
    assert first != second
    assert decrypt_value("WHM_API_TOKEN", first) == "same-secret"
    with pytest.raises(IntegrationSecretError):
        decrypt_value("STRIPE_SECRET_KEY", first)


@pytest.mark.parametrize("value", ["plaintext", "enc:v2:future", "enc:v1:unknown:AAAA", "enc:v1:test-v1:!!!", "enc:v1:test-v1:AAAA"])
def test_unreadable_rows_never_select_environment_fallback(settings, value):
    settings.WHM_API_TOKEN = "fallback-must-not-be-used"
    IntegrationSetting.set_value("WHM_API_TOKEN", "original")
    row = IntegrationSetting.objects.get(key="WHM_API_TOKEN")
    raw_value(row.pk, value)
    cache.set("runtime_setting:WHM_API_TOKEN", "old-cached-secret")
    with pytest.raises(IntegrationSecretError):
        get_runtime_setting("WHM_API_TOKEN", "default-secret")
    with pytest.raises(IntegrationSecretError):
        IntegrationSetting.get_value("WHM_API_TOKEN", "default-secret")


def test_tampered_ciphertext_is_rejected():
    value = encrypt_value("WHM_API_TOKEN", "secret")
    header, encoded = value.rsplit(":", 1)
    blob = bytearray(base64.b64decode(encoded))
    blob[-1] ^= 1
    damaged = header + ":" + base64.b64encode(blob).decode()
    with pytest.raises(IntegrationSecretError):
        decrypt_value("WHM_API_TOKEN", damaged)


def test_explicit_blank_is_not_environment_fallback(settings):
    settings.WHM_API_TOKEN = "environment-token"
    IntegrationSetting.set_value("WHM_API_TOKEN", "")
    assert get_runtime_setting_with_source("WHM_API_TOKEN", "default") == ("", "database")


def test_secret_reads_do_not_populate_plaintext_cache():
    cache.clear()
    IntegrationSetting.set_value("WHM_API_TOKEN", "cache-private")
    assert get_runtime_setting("WHM_API_TOKEN") == "cache-private"
    assert cache.get("runtime_setting:WHM_API_TOKEN") is None


def test_database_outage_is_not_an_environment_fallback(monkeypatch, settings):
    settings.WHM_API_TOKEN = "environment-token"
    def failed_query(*args, **kwargs):
        raise OperationalError("Unavailable")
    monkeypatch.setattr(IntegrationSetting.objects, "filter", failed_query)
    with pytest.raises(OperationalError):
        get_runtime_setting("WHM_API_TOKEN")


@pytest.mark.parametrize("keys,active", [({}, ""), ({"bad:id": OLD_KEY}, "bad:id"), ({"old": "invalid"}, "old"), ({"old": base64.b64encode(b"short").decode()}, "old")])
def test_bad_key_configuration_cannot_write_secrets(keys, active):
    with override_settings(INTEGRATION_ENCRYPTION_KEYS=keys, INTEGRATION_ENCRYPTION_ACTIVE_KEY=active):
        with pytest.raises(IntegrationSecretError):
            IntegrationSetting.set_value("WHM_API_TOKEN", "not-persisted")
    assert not IntegrationSetting.objects.exists()


def test_guarded_orm_bulk_writes_cannot_store_plaintext():
    IntegrationSetting.set_value("WHM_API_TOKEN", "original")
    with pytest.raises(IntegrationSecretError):
        IntegrationSetting.objects.update(value="plaintext")
    with pytest.raises(IntegrationSecretError):
        IntegrationSetting.objects.bulk_create([IntegrationSetting(key="NEW_SECRET", value="plaintext")])
    assert IntegrationSetting.get_value("WHM_API_TOKEN") == "original"


def test_conversion_is_read_only_by_default_resumable_and_verified():
    IntegrationSetting.set_value("WHM_API_TOKEN", "temporary")
    row = IntegrationSetting.objects.get()
    raw_value(row.pk, "historical-plaintext")
    output = io.StringIO()
    call_command("encrypt_integration_settings", stdout=output)
    assert "historical-plaintext" not in output.getvalue()
    assert raw_value(row.pk) == "historical-plaintext"
    with pytest.raises(CommandError, match="incomplete"):
        call_command("encrypt_integration_settings", check=True, stdout=io.StringIO())
    call_command("encrypt_integration_settings", apply=True, batch_size=1, stdout=io.StringIO())
    encrypted = raw_value(row.pk)
    assert encrypted.startswith(PREFIX)
    assert IntegrationSetting.get_value(row.key) == "historical-plaintext"
    call_command("encrypt_integration_settings", apply=True, stdout=io.StringIO())
    assert raw_value(row.pk) == encrypted
    call_command("encrypt_integration_settings", check=True, stdout=io.StringIO())


def test_rotation_requires_old_key_then_rewraps_with_active_key():
    with override_settings(INTEGRATION_ENCRYPTION_KEYS={"old": OLD_KEY}, INTEGRATION_ENCRYPTION_ACTIVE_KEY="old"):
        IntegrationSetting.set_value("WHM_API_TOKEN", "rotate-secret")
    with override_settings(INTEGRATION_ENCRYPTION_KEYS={"old": OLD_KEY, "new": NEW_KEY}, INTEGRATION_ENCRYPTION_ACTIVE_KEY="new"):
        assert get_runtime_setting("WHM_API_TOKEN") == "rotate-secret"
        call_command("encrypt_integration_settings", apply=True, rotate=True, stdout=io.StringIO())
    with override_settings(INTEGRATION_ENCRYPTION_KEYS={"new": NEW_KEY}, INTEGRATION_ENCRYPTION_ACTIVE_KEY="new"):
        assert get_runtime_setting("WHM_API_TOKEN") == "rotate-secret"
        call_command("encrypt_integration_settings", check=True, stdout=io.StringIO())


def test_conversion_stops_at_unreadable_row_and_keeps_prior_commits():
    first, _ = IntegrationSetting.set_value("FIRST", "temporary")
    second, _ = IntegrationSetting.set_value("SECOND", "temporary")
    raw_value(first.pk, "legacy")
    raw_value(second.pk, "enc:v1:unknown:AAAA")
    with pytest.raises(CommandError, match=f"row {second.pk}"):
        call_command("encrypt_integration_settings", apply=True, stdout=io.StringIO())
    assert IntegrationSetting.get_value("FIRST") == "legacy"
    assert raw_value(second.pk) == "enc:v1:unknown:AAAA"


def test_password_backup_roundtrip_reencrypts_for_current_storage_key():
    IntegrationSetting.set_value("WHM_API_TOKEN", "backup-secret")
    backup = build_backup_zip("long-backup-password")
    assert b"backup-secret" not in backup
    IntegrationSetting.objects.all().delete()
    with override_settings(INTEGRATION_ENCRYPTION_KEYS={"new": NEW_KEY}, INTEGRATION_ENCRYPTION_ACTIVE_KEY="new"):
        import_backup_zip(backup, "long-backup-password")
        assert get_runtime_setting("WHM_API_TOKEN") == "backup-secret"
        assert IntegrationSetting.objects.get().value.startswith("enc:v1:new:")


def test_missing_key_returns_safe_http_failure_without_credential_fallback(client, django_user_model):
    login_owner(client, django_user_model.objects.create_user(email="secret-owner@example.test"))
    IntegrationSetting.set_value("SITE_NAME", "secret-setting-value")
    with override_settings(INTEGRATION_ENCRYPTION_KEYS={}, INTEGRATION_ENCRYPTION_ACTIVE_KEY=""):
        response = client.get(reverse("admin_tools:settings_overview"))
    assert response.status_code == 503
    assert b"secret-setting-value" not in response.content
    assert b"enc:v1" not in response.content
    assert response["Cache-Control"] == "no-store"


def test_setting_changes_audit_names_without_values_and_fail_atomically(django_user_model, monkeypatch):
    from apps.audit.models import AuditLog
    actor = django_user_model.objects.create_user(email="audit-settings@example.test")
    IntegrationSetting.set_value("WHM_API_TOKEN", "private-value", actor=actor)
    entry = AuditLog.objects.get(action="integration.setting.updated")
    assert entry.user == actor
    assert entry.data["key"] == "WHM_API_TOKEN"
    assert "private-value" not in str(entry.data)
    def unavailable(*args, **kwargs):
        raise RuntimeError("Audit unavailable")
    monkeypatch.setattr(AuditLog.objects, "create", unavailable)
    with pytest.raises(RuntimeError, match="Audit unavailable"):
        IntegrationSetting.set_value("WHM_API_TOKEN", "must-rollback", actor=actor)
    assert IntegrationSetting.get_value("WHM_API_TOKEN") == "private-value"


def test_conversion_can_bootstrap_from_a_fresh_process_with_legacy_storage(tmp_path):
    import os
    from pathlib import Path
    import sqlite3
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "conversion.sqlite3"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = "sqlite:///" + database.as_posix()
    environment["DJANGO_SETTINGS_MODULE"] = "cyberask_domains.settings.test"
    def run(*arguments):
        result = subprocess.run([sys.executable, "manage.py", *arguments, "--settings=cyberask_domains.settings.test"],
                                cwd=root, env=environment, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert "bootstrap-private-token" not in result.stdout + result.stderr
    run("migrate", "--noinput", "--verbosity=0")
    with sqlite3.connect(database) as raw:
        raw.execute("INSERT INTO admin_tools_integrationsetting (key,value,is_secret,created_at,updated_at) VALUES (?,?,?,?,?)",
                    ("WHM_API_TOKEN", "bootstrap-private-token", 1, "2026-10-27 00:00:00", "2026-10-27 00:00:00"))
    run("encrypt_integration_settings", "--apply")
    run("encrypt_integration_settings", "--check")
    with sqlite3.connect(database) as raw:
        stored = raw.execute("SELECT value FROM admin_tools_integrationsetting").fetchone()[0]
    assert stored.startswith(PREFIX)
    assert "bootstrap-private-token" not in stored


def test_registrar_form_never_renders_stored_or_posted_api_key(client, django_user_model):
    login_owner(client, django_user_model.objects.create_user(email="redaction-owner@example.test"))
    IntegrationSetting.set_value("RESELLERCLUB_API_KEY", "stored-private-registrar-key")
    url = reverse("admin_tools:settings_setup_step", kwargs={"step_key": "registrar"})
    assert b"stored-private-registrar-key" not in client.get(url).content
    response = client.post(url, {"resellerclub_api_key": "posted-private-key", "resellerclub_api_mode": "custom"})
    assert response.status_code == 200
    assert b"posted-private-key" not in response.content
