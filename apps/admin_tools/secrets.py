"""Versioned AES-GCM envelopes bound to the integration setting's identity."""
import base64
import binascii
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

PREFIX = "enc:v1:"


class IntegrationSecretError(Exception):
    """Safe message only: never attach plaintext, ciphertext or key material."""


def keyring():
    raw = getattr(settings, "INTEGRATION_ENCRYPTION_KEYS", {})
    active = getattr(settings, "INTEGRATION_ENCRYPTION_ACTIVE_KEY", "")
    if not isinstance(raw, dict) or not isinstance(active, str) or active not in raw:
        raise IntegrationSecretError("Integration encryption keys are not configured.")
    keys = {}
    for identifier, encoded in raw.items():
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", identifier):
            raise IntegrationSecretError("Integration encryption key identifiers are invalid.")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError, binascii.Error):
            raise IntegrationSecretError("Integration encryption key encoding is invalid.") from None
        if len(key) != 32:
            raise IntegrationSecretError("Integration encryption keys must contain 32 bytes.")
        keys[identifier] = key
    return active, keys


def encrypt_value(setting_key, plaintext):
    if not isinstance(plaintext, str):
        raise IntegrationSecretError("Integration setting values must be strings.")
    active, keys = keyring()
    nonce = os.urandom(12)
    aad = f"integration-setting:v1:{active}:{setting_key}".encode("utf-8")
    encrypted = AESGCM(keys[active]).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return PREFIX + active + ":" + base64.b64encode(nonce + encrypted).decode("ascii")


def decrypt_value(setting_key, stored):
    if not isinstance(stored, str) or not stored.startswith(PREFIX):
        raise IntegrationSecretError("A legacy integration setting requires encrypted-storage conversion.")
    active, keys = keyring()
    try:
        identifier, encoded = stored[len(PREFIX):].split(":", 1)
        key = keys[identifier]
        blob = base64.b64decode(encoded, validate=True)
        if len(blob) < 28:
            raise ValueError
        aad = f"integration-setting:v1:{identifier}:{setting_key}".encode("utf-8")
        return AESGCM(key).decrypt(blob[:12], blob[12:], aad).decode("utf-8")
    except (KeyError, ValueError, TypeError, binascii.Error, InvalidTag, UnicodeDecodeError):
        raise IntegrationSecretError("An integration setting could not be authenticated or decrypted.") from None
