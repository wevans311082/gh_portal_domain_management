"""Password-protected configuration backup using AES-GCM inside a zip file."""
from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from io import BytesIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

from apps.admin_tools.models import IntegrationSetting

BACKUP_FORMAT = "cyberask-config-v1"
_KDF_ITERATIONS = 390_000


class ConfigBackupError(Exception):
    pass


def _derive_key(password: str, salt: bytes) -> bytes:
    if not password or len(password) < 8:
        raise ConfigBackupError("Password must be at least 8 characters.")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_KDF_ITERATIONS)
    return kdf.derive(password.encode("utf-8"))


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
    for item in IntegrationSetting.objects.order_by("key"):
        rows.append(
            {
                "key": item.key,
                "value": item.value,
                "is_secret": bool(item.is_secret),
            }
        )
    return {
        "format": BACKUP_FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "settings": rows,
    }


def build_backup_zip(password: str) -> bytes:
    payload = json.dumps(export_settings_payload(), indent=2).encode("utf-8")
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
                {
                    "format": BACKUP_FORMAT,
                    "encrypted": True,
                    "algorithm": "AES-256-GCM",
                    "kdf": "PBKDF2-HMAC-SHA256",
                    "iterations": _KDF_ITERATIONS,
                },
                indent=2,
            ),
        )
        zf.writestr("payload.bin", encrypted)
    return buffer.getvalue()


def import_backup_zip(file_bytes: bytes, password: str) -> int:
    try:
        with zipfile.ZipFile(BytesIO(file_bytes), "r") as zf:
            names = zf.namelist()
            if "payload.bin" not in names:
                raise ConfigBackupError("Zip is missing payload.bin.")
            blob = zf.read("payload.bin")
    except zipfile.BadZipFile as exc:
        raise ConfigBackupError("File is not a valid zip archive.") from exc

    raw = _decrypt(blob, password)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigBackupError("Decrypted payload is not valid JSON.") from exc

    if data.get("format") != BACKUP_FORMAT:
        raise ConfigBackupError("Unrecognised backup format.")

    settings_rows = data.get("settings") or []
    imported = 0
    for row in settings_rows:
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        IntegrationSetting.set_value(
            key,
            str(row.get("value") or ""),
            is_secret=bool(row.get("is_secret", True)),
        )
        imported += 1
    return imported
