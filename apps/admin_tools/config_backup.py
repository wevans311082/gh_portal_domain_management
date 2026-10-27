"""Password-protected configuration backup using AES-GCM inside a zip file."""
from __future__ import annotations

import json
import os
import stat
import zipfile
import zlib
from datetime import datetime, timezone
from io import BytesIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from django.db import transaction

from apps.admin_tools.models import IntegrationSetting
from .setting_schema import SETTING_SCHEMA, SettingValidationError, validate_setting

BACKUP_FORMAT = "cyberask-config-v1"
_KDF_ITERATIONS = 390_000
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_COMPRESSION_RATIO = 50
MEMBER_LIMITS = {"README.txt": 8192, "manifest.json": 4096, "payload.bin": MAX_PAYLOAD_BYTES + 44}
MANIFEST = {"format": BACKUP_FORMAT, "encrypted": True, "algorithm": "AES-256-GCM",
            "kdf": "PBKDF2-HMAC-SHA256", "iterations": _KDF_ITERATIONS}


class ConfigBackupError(Exception):
    pass


def _derive_key(password: str, salt: bytes) -> bytes:
    _validate_password(password)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_KDF_ITERATIONS)
    return kdf.derive(password.encode("utf-8"))


def _validate_password(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
        raise ConfigBackupError("Password must contain 8 to 1024 characters.")
    try:
        password.encode("utf-8")
    except UnicodeError:
        raise ConfigBackupError("Password contains invalid characters.") from None


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigBackupError("Backup JSON contains duplicate keys.")
        result[key] = value
    return result


def _load_json(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object,
                          parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ConfigBackupError("Backup contains invalid JSON.") from None


def _reject_constant(value):
    raise ValueError("Non-finite JSON numbers are unsupported")


def read_backup_upload(upload):
    if upload.size > MAX_ARCHIVE_BYTES:
        raise ConfigBackupError("Backup zip must be 2 MB or smaller.")
    data = upload.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ConfigBackupError("Backup zip must be 2 MB or smaller.")
    return data


def _encrypt(payload: bytes, password: str) -> bytes:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(password, salt)
    ciphertext = AESGCM(key).encrypt(nonce, payload, None)
    return salt + nonce + ciphertext


def _decrypt(blob: bytes, password: str) -> bytes:
    if len(blob) < 16 + 12 + 16:
        raise ConfigBackupError("Backup payload is truncated or corrupt.")
    salt, nonce, ciphertext = blob[:16], blob[16:28], blob[28:]
    key = _derive_key(password, salt)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ConfigBackupError("Could not decrypt backup. Check the password.") from exc


def export_settings_payload() -> dict:
    rows = []
    for item in IntegrationSetting.objects.order_by("key")[:len(SETTING_SCHEMA) + 1]:
        try:
            value, secret = validate_setting(item.key, item.plaintext_value)
        except SettingValidationError as error:
            raise ConfigBackupError(str(error)) from None
        rows.append(
            {
                "key": item.key,
                "value": value,
                "is_secret": secret,
            }
        )
    return {
        "format": BACKUP_FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "settings": rows,
    }


def build_backup_zip(password: str) -> bytes:
    _validate_password(password)
    payload = json.dumps(export_settings_payload(), indent=2).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ConfigBackupError("Backup payload exceeds the supported size.")
    encrypted = _encrypt(payload, password)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "README.txt",
            (
                "CyberAsk configuration backup\n\n"
                "This zip contains an AES-GCM encrypted payload. Import it from\n"
                "Admin Tools → Settings using the same password used to export.\n"
                "It cannot be opened in 7-Zip/WinZip because the encryption is\n"
                "application-level, not ZipCrypto.\n"
            ),
        )
        zf.writestr(
            "manifest.json",
            json.dumps(
                MANIFEST,
                indent=2,
            ),
        )
        zf.writestr("payload.bin", encrypted)
    return buffer.getvalue()


def import_backup_zip(file_bytes: bytes, password: str, actor=None) -> int:
    _validate_password(password)
    if not isinstance(file_bytes, bytes) or len(file_bytes) > MAX_ARCHIVE_BYTES:
        raise ConfigBackupError("Backup zip must be 2 MB or smaller.")
    try:
        with zipfile.ZipFile(BytesIO(file_bytes), "r") as zf:
            entries = zf.infolist()
            if len(entries) != 3 or {entry.filename for entry in entries} != set(MEMBER_LIMITS):
                raise ConfigBackupError("Backup zip must contain exactly README.txt, manifest.json and payload.bin.")
            if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
                raise ConfigBackupError("Backup archive expansion exceeds the supported size.")
            members = {}
            for entry in entries:
                limit = MEMBER_LIMITS[entry.filename]
                file_type = stat.S_IFMT(entry.external_attr >> 16)
                if (entry.is_dir() or entry.orig_filename != entry.filename or file_type not in {0, stat.S_IFREG} or entry.flag_bits & 1
                        or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or entry.file_size > limit or entry.file_size > max(1, entry.compress_size) * MAX_COMPRESSION_RATIO):
                    raise ConfigBackupError("Backup zip contains an unsafe or oversized entry.")
                with zf.open(entry) as member:
                    content = member.read(limit + 1)
                if len(content) > limit or len(content) != entry.file_size:
                    raise ConfigBackupError("Backup zip entry size is invalid.")
                members[entry.filename] = content
            manifest = _load_json(members["manifest.json"])
            if (not isinstance(manifest, dict) or manifest != MANIFEST or type(manifest.get("encrypted")) is not bool
                    or type(manifest.get("iterations")) is not int):
                raise ConfigBackupError("Backup manifest is unsupported.")
            blob = members["payload.bin"]
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError, ValueError, zlib.error):
        raise ConfigBackupError("File is not a valid supported backup zip.") from None

    raw = _decrypt(blob, password)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ConfigBackupError("Decrypted backup exceeds the supported size.")
    data = _load_json(raw)
    if not isinstance(data, dict) or set(data) != {"format", "exported_at", "settings"} or data["format"] != BACKUP_FORMAT:
        raise ConfigBackupError("Unrecognised backup structure or format.")
    try:
        if not isinstance(data["exported_at"], str) or len(data["exported_at"]) > 64:
            raise ValueError
        exported_at = datetime.fromisoformat(data["exported_at"])
        if exported_at.tzinfo is None:
            raise ValueError
    except ValueError:
        raise ConfigBackupError("Backup export timestamp is invalid.") from None
    settings_rows = data["settings"]
    if not isinstance(settings_rows, list) or len(settings_rows) > len(SETTING_SCHEMA):
        raise ConfigBackupError("Backup setting count or structure is invalid.")
    validated = []
    seen = set()
    for row in settings_rows:
        if not isinstance(row, dict) or set(row) != {"key", "value", "is_secret"} or type(row["is_secret"]) is not bool:
            raise ConfigBackupError("Backup setting structure is invalid.")
        try:
            value, secret = validate_setting(row["key"], row["value"])
        except SettingValidationError as error:
            raise ConfigBackupError(str(error)) from None
        if row["key"] in seen:
            raise ConfigBackupError("Backup contains duplicate settings.")
        seen.add(row["key"])
        validated.append((row["key"], value, secret))
    # Complete validation precedes any write. Encryption/audit/database failure
    # rolls every imported row back, including its associated audit entries.
    with transaction.atomic():
        for key, value, secret in validated:
            IntegrationSetting.set_value(key, value, is_secret=secret, actor=actor)
    return len(validated)
