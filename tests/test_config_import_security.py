"""SEC-08: bounded archives, typed schemas, encryption and atomic import."""
import io
import json
import stat
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.admin_tools import config_backup as backup
from apps.admin_tools.models import IntegrationSetting
from apps.admin_tools.setting_schema import SETTING_SCHEMA
from apps.audit.models import AuditLog
from tests.security_helpers import login_owner

pytestmark = pytest.mark.django_db
PASSWORD = "valid-backup-password"


def row(key="WHM_HOST", value="restored.example", secret=False):
    return {"key": key, "value": value, "is_secret": secret}


def payload(rows=None):
    return {"format": backup.BACKUP_FORMAT, "exported_at": "2026-10-27T12:00:00+00:00", "settings": [row()] if rows is None else rows}


def archive(data=None, raw=None, manifest=None, members=None, compression=zipfile.ZIP_DEFLATED):
    if members is None:
        raw = json.dumps(payload() if data is None else data).encode() if raw is None else raw
        members = [("README.txt", b"Configuration backup"),
                   ("manifest.json", json.dumps(backup.MANIFEST if manifest is None else manifest).encode()),
                   ("payload.bin", backup._encrypt(raw, PASSWORD))]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as zipped:
        for name, content in members:
            zipped.writestr(name, content)
    return output.getvalue()


def test_schema_covers_existing_wizard_and_graph_settings():
    from apps.admin_tools.wizard_views import _STEP_ENV_KEYS
    from apps.admin_tools.forms import Microsoft365GraphSettingsForm
    assert {key for mapping in _STEP_ENV_KEYS.values() for key in mapping.values()} <= set(SETTING_SCHEMA)
    assert {key for key, secret in Microsoft365GraphSettingsForm.SETTING_MAP.values()} <= set(SETTING_SCHEMA)


def test_normal_backup_restores_encrypted_values_and_canonical_secret_flags(django_user_model):
    actor = django_user_model.objects.create_user(email="import-actor@example.test")
    data = archive(payload([row(), row("WHM_API_TOKEN", "private-restored-token", False), row("EMAIL_USE_TLS", "True")]))
    assert backup.import_backup_zip(data, PASSWORD, actor=actor) == 3
    token = IntegrationSetting.objects.get(key="WHM_API_TOKEN")
    assert token.is_secret
    assert token.value.startswith("enc:v1:")
    assert "private-restored-token" not in token.value
    assert token.plaintext_value == "private-restored-token"
    assert IntegrationSetting.get_value("EMAIL_USE_TLS") == "true"
    audits = AuditLog.objects.filter(action="integration.setting.updated")
    assert audits.count() == 3
    assert all(entry.user == actor and "private-restored-token" not in str(entry.data) for entry in audits)


@pytest.mark.parametrize("key,value", [
    ("SECRET_KEY", "arbitrary"), ("DATABASE_URL", "arbitrary"), ("REDIS_URL", "arbitrary"),
    ("TRUSTED_PROXY_CIDRS", "0.0.0.0/0"), ("INTEGRATION_ENCRYPTION_KEYS", "arbitrary"),
    ("UNRECOGNIZED_KEY", "arbitrary"), ("FEATURE_UNKNOWN", "true"),
    ("WHM_PORT", "65536"), ("EMAIL_PORT", "0"), ("EMAIL_PORT", "12.5"),
    ("EMAIL_USE_TLS", "yes"), ("EMAIL_USE_TLS", True), ("WHM_HOST", ["host"]),
    ("M365_GRAPH_TIMEOUT_SECONDS", "121"), ("M365_GRAPH_TIMEOUT_SECONDS", "2"),
    ("GOCARDLESS_ENVIRONMENT", "production"), ("DJANGO_TIME_ZONE", "Not/AZone"),
    ("DJANGO_ADMIN_URL", "../escape/"), ("RESELLERCLUB_API_URL", "http://example.test/api"),
    ("RESELLERCLUB_API_URL", "https://user:password@example.test/api"),
    ("RESELLERCLUB_API_URL", "https://example.test/api?api-key=secret"),
    ("M365_GRAPH_DEFAULT_MAILBOX", "not-email"), ("RESELLERCLUB_CUSTOMER_ID", "abc"),
    ("REGISTRAR_LOW_BALANCE_THRESHOLD", "NaN"), ("REGISTRAR_LOW_BALANCE_THRESHOLD", "-1"),
    ("REGISTRAR_LOW_BALANCE_THRESHOLD", "1.001"), ("WHM_NAMESERVERS", "ns1.example,ns1.example"),
    ("WHM_HOST", "https://bad-host/path"), ("SITE_NAME", "x" * 101),
    ("WHM_API_TOKEN", "x" * 4097), ("WHM_API_TOKEN", "secret\nsecond-line"),
    ("WHM_API_TOKEN", "\ud800"),
])
def test_unknown_or_invalid_values_reject_entire_backup(key, value):
    IntegrationSetting.set_value("WHM_HOST", "original.example")
    AuditLog.objects.all().delete()
    data = archive(payload([row(), row(key, value)]))
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(data, PASSWORD)
    assert IntegrationSetting.get_value("WHM_HOST") == "original.example"
    assert IntegrationSetting.objects.count() == 1
    assert not AuditLog.objects.exists()


@pytest.mark.parametrize("data", [
    [], None, {"format": backup.BACKUP_FORMAT}, {**payload(), "extra": True},
    {**payload(), "settings": {}}, {**payload(), "settings": [None]},
    {**payload(), "settings": [{**row(), "extra": True}]},
    {**payload(), "settings": [{**row(), "is_secret": "false"}]},
    {**payload(), "settings": [row(), row()]},
    {**payload(), "settings": [row()] * (len(SETTING_SCHEMA) + 1)},
    {**payload(), "exported_at": "yesterday"}, {**payload(), "exported_at": "2026-10-27"},
])
def test_payload_structure_and_duplicates_are_strict(data):
    # Explicit raw encoding also permits testing a JSON null root.
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(archive(raw=json.dumps(data).encode()), PASSWORD)
    assert not IntegrationSetting.objects.exists()


@pytest.mark.parametrize("raw", [
    b'{"format":"first","format":"second"}',
    json.dumps(payload()).replace('"value": "restored.example"', '"value":"first","value":"second"').encode(),
    b'{"value":NaN}', b"\xff", b"[" * 1200 + b"]" * 1200,
])
def test_duplicate_json_keys_nonfinite_values_invalid_encoding_and_depth_are_rejected(raw):
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(archive(raw=raw), PASSWORD)
    assert not IntegrationSetting.objects.exists()


@pytest.mark.parametrize("members", [
    [], [("payload.bin", b"fake")], [("payload.bin", b"one"), ("payload.bin", b"two"), ("manifest.json", b"{}")],
    [("README.txt", b"readme"), ("manifest.json", b"{}"), ("../payload.bin", b"fake")],
    [("README.txt", b"readme"), ("manifest.json", b"{}"), ("payload.bin", b"fake"), ("extra.txt", b"extra")],
    [("README.txt", b"x" * 9000), ("manifest.json", b"{}"), ("payload.bin", b"fake")],
    [("README.txt", b"x" * 8000), ("manifest.json", b"{}"), ("payload.bin", b"fake")],
    [("README.txt", b"readme"), ("manifest.json", b"{}"), ("payload.bin", b"x" * (backup.MAX_PAYLOAD_BYTES + 45))],
])
def test_archive_shape_expansion_and_ratio_are_checked_before_decryption(members, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsafe archive reached decryption")
    monkeypatch.setattr(backup, "_decrypt", forbidden)
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(archive(members=members), PASSWORD)


@pytest.mark.parametrize("manifest", [{}, {**backup.MANIFEST, "iterations": 1}, {**backup.MANIFEST, "extra": True}, {**backup.MANIFEST, "encrypted": 1}])
def test_manifest_cannot_choose_algorithm_or_unbounded_kdf(manifest):
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(archive(manifest=manifest), PASSWORD)
    assert not IntegrationSetting.objects.exists()


def test_symlink_members_are_rejected():
    info = zipfile.ZipInfo("README.txt")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    data = archive(members=[(info, b"target"), ("manifest.json", b"{}"), ("payload.bin", b"fake")])
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(data, PASSWORD)


def test_corrupt_archive_and_ciphertext_do_not_write():
    data = archive()
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(data[:len(data) // 2], PASSWORD)
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        members = [(name, zipped.read(name)) for name in zipped.namelist()]
    members[-1] = ("payload.bin", members[-1][1][:-1] + bytes([members[-1][1][-1] ^ 1]))
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(archive(members=members), PASSWORD)
    assert not IntegrationSetting.objects.exists()


def test_oversized_upload_is_bounded_even_when_size_metadata_lies():
    upload = SimpleUploadedFile("backup.zip", b"x" * (backup.MAX_ARCHIVE_BYTES + 1))
    with pytest.raises(backup.ConfigBackupError):
        backup.read_backup_upload(upload)
    upload.size = 1
    with pytest.raises(backup.ConfigBackupError):
        backup.read_backup_upload(upload)
    assert upload.tell() == backup.MAX_ARCHIVE_BYTES + 1


def test_mid_import_failure_rolls_back_values_and_audit_entries(monkeypatch):
    IntegrationSetting.set_value("WHM_HOST", "original.example")
    AuditLog.objects.all().delete()
    real_save = IntegrationSetting.set_value
    def fail_second(key, *args, **kwargs):
        if key == "WHM_API_TOKEN":
            raise RuntimeError("Simulated persistence failure")
        return real_save(key, *args, **kwargs)
    monkeypatch.setattr(IntegrationSetting, "set_value", fail_second)
    with pytest.raises(RuntimeError, match="Simulated"):
        backup.import_backup_zip(archive(payload([row(), row("WHM_API_TOKEN", "secret")])), PASSWORD)
    assert IntegrationSetting.get_value("WHM_HOST") == "original.example"
    assert IntegrationSetting.objects.count() == 1
    assert not AuditLog.objects.exists()


def test_unknown_existing_keys_cannot_produce_an_unrestorable_export():
    IntegrationSetting.set_value("UNREGISTERED", "value")
    with pytest.raises(backup.ConfigBackupError, match="unsupported setting"):
        backup.build_backup_zip(PASSWORD)


def test_http_import_enforces_current_owner_mfa_and_preserves_valid_settings(client, django_user_model):
    actor = django_user_model.objects.create_user(email="http-import-owner@example.test")
    login_owner(client, actor)
    data = archive()
    response = client.post(reverse("admin_tools:settings_import"), {
        "backup_password": PASSWORD, "backup_file": SimpleUploadedFile("backup.zip", data),
    })
    assert response.status_code == 302
    assert IntegrationSetting.get_value("WHM_HOST") == "restored.example"
    assert AuditLog.objects.get(action="integration.setting.updated").user == actor
    session = client.session
    session["_mfa_verified_at"] = 0
    session.save()
    response = client.post(reverse("admin_tools:settings_import"), {
        "backup_password": PASSWORD, "backup_file": SimpleUploadedFile("backup.zip", archive(payload([row(value="denied.example")]))),
    })
    assert response.status_code == 403
    assert IntegrationSetting.get_value("WHM_HOST") == "restored.example"


@pytest.mark.parametrize("password", ["short", "x" * 1025, None])
def test_password_limits_apply_before_archive_processing(password):
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(b"not-a-zip", password)
    with pytest.raises(backup.ConfigBackupError):
        backup.build_backup_zip(password)


def test_unsupported_compression_and_crc_damage_are_rejected():
    members = [("README.txt", b"readme"), ("manifest.json", b"{}"), ("payload.bin", b"fake")]
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(archive(members=members, compression=zipfile.ZIP_BZIP2), PASSWORD)
    damaged = bytearray(archive(members=members, compression=zipfile.ZIP_STORED))
    damaged[damaged.find(b"readme")] ^= 1
    with pytest.raises(backup.ConfigBackupError):
        backup.import_backup_zip(bytes(damaged), PASSWORD)


def test_archive_byte_limit_is_checked_before_zip_parsing():
    with pytest.raises(backup.ConfigBackupError, match="2 MB"):
        backup.import_backup_zip(b"x" * (backup.MAX_ARCHIVE_BYTES + 1), PASSWORD)
