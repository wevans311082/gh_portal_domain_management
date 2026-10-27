# Configuration backup and restore contract — SEC-08

## Format and limits

The existing `cyberask-config-v1` archive remains the supported format. It contains exactly three regular files at the archive root: README.txt, manifest.json and payload.bin. The payload is password-encrypted with AES-256-GCM and PBKDF2-HMAC-SHA256 at 390,000 iterations. Salt and nonce are generated independently for every export. Manifest algorithm/KDF parameters are fixed; imports cannot select an expensive KDF or downgrade encryption.

| Limit | Value |
|---|---|
| Uploaded/archive bytes | 2 MB |
| Total expanded ZIP bytes | 2 MB |
| Plaintext JSON payload | 1 MB |
| Encrypted payload member | 1 MB + 44 bytes of cryptographic overhead |
| README | 8 KB |
| Manifest | 4 KB |
| Maximum compression ratio per member | 50:1 |
| ZIP methods | Stored or Deflate |
| Password length | 8–1024 characters |
| Setting count | At most the number of explicitly registered setting keys |

Duplicates, extra/missing/path-bearing entries, directories, symlinks, ZIP-level encryption, invalid CRC/compressed data and unsupported methods are rejected. Member metadata is checked before reading, and actual reads are bounded independently. No archive file is extracted to disk.

## Payload and setting schema

The payload has exactly `format`, `exported_at` and `settings`. The timestamp must be an ISO timestamp with timezone information. Each setting has exactly `key`, string `value` and boolean `is_secret`. Duplicate JSON keys, repeated settings, non-finite JSON constants, invalid UTF-8, malformed structures and excessive nesting are rejected.

The authoritative list is `apps/admin_tools/setting_schema.py`. It covers existing wizard/Graph application settings, the supported feature flags, registrar balance threshold and hosting nameservers. Values have bounded lengths, single-line text constraints and key-specific validation. Examples include ports 1–65535, Graph timeouts 3–120 seconds, finite nonnegative monetary thresholds with at most two decimal places, known timezone identifiers, valid addresses and HTTPS provider endpoints without embedded credentials/query strings.

The archive cannot create arbitrary configuration keys. Django secret keys, database/Redis URLs, trusted-proxy configuration, storage-encryption keys and unrecognized feature flags are not portable through this interface. Some legacy wizard preferences refer to bootstrap settings; restoring those preferences does not rewrite external process configuration or change a running route/timezone automatically.

The registry determines the stored secret flag. An archive cannot downgrade a credential by declaring it public. All integration rows are encrypted at rest regardless of that flag. Intentional blank credentials are preserved as blank rather than selecting an environment fallback.

## Atomic restore and failure behaviour

Only a recently MFA-verified platform owner may import/export through Admin Tools. Import validates archive structure, decrypts/authenticates, parses JSON and validates every setting before beginning writes. The entire write set and its field-name-only audit entries are transactional. An invalid row or persistence/encryption/audit failure preserves the original configuration. A successful restore replaces matching keys, creates supported missing keys and leaves unrelated existing rows alone.

Imported plaintext exists only in request memory and is re-encrypted under the destination's active storage key. The backup password is independent of the storage key, allowing a protected export to be restored into a system using another storage key. Missing storage-key configuration fails explicitly; it never saves plaintext. The archive is not a database, customer-data or encryption-key backup.

Exports validate the same schema. An unsupported or invalid current setting produces a safe error instead of an unusable archive. Older archives that relied on arbitrary keys, invalid values, duplicate entries, incomplete structure or unbounded payloads are intentionally rejected. Correctly structured existing v1 exports within these limits remain compatible.

## Verification and operational boundary

Security tests exercise malformed archives, payload types, duplicates, real encryption/tampering, bounded reads, schema inventory, atomic rollback and owner confirmation. Browser fixtures exercise normal and rejected-import screens at desktop/mobile sizes. See [implementation status](implementation-status.md) for measured results.

The application limits uploaded-file processing after Django has accepted the request. Proxy/body transport limits and server configuration remain external. Protect exported archives and passwords separately under the retention/recovery policy. Existing-database encryption conversion, external-key provisioning, provider rotation and historical backup cleanup remain the separately documented [SEC-05 prerequisites](encrypted-settings.md); this import change does not perform those operations.
