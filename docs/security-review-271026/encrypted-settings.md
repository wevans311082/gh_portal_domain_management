# Encrypted integration settings — SEC-05

## Storage and access contract

Application writes persist `enc:v1:<key identifier>:<base64 nonce and authenticated ciphertext>` in the existing value column. AES-256-GCM authenticates the setting name and key identifier as associated data. Every value is protected regardless of the presentation flag, preventing misclassified secrets from leaking in database dumps. Querying model `.value` returns stored ciphertext; use `plaintext_value`, `IntegrationSetting.get_value` or the common runtime accessor when an authorized application service needs the value.

Normal application access must use `set_value`, which encrypts and records field names/actor without logging values. HTTP access is limited by the staff owner/recent-MFA policy. Native admin is read-only for these rows. ORM bulk value/key/secret-flag updates are rejected; database-owner/raw SQL access remains privileged and is not an application authorization boundary.

An absent database row can fall back to environment/default configuration. An existing blank value means explicitly blank. Existing unreadable ciphertext, legacy plaintext and database outages do not cause credential fallback. Encryption errors produce generic HTTP 503 responses with a reference; internal diagnostics omit values and keys. Plaintext settings are no longer cached by the runtime accessor.

## Key configuration

Provide `INTEGRATION_ENCRYPTION_KEYS` as a JSON object mapping short immutable identifiers to standard-base64 32-byte random keys. Provide `INTEGRATION_ENCRYPTION_ACTIVE_KEY` as the identifier used for new writes. Generate keys with a cryptographically secure generator or managed secret system. Keep them outside the database and source control, with separately controlled recovery copies. Do not use the public key in test settings outside isolated tests.

The normal application has no default encryption key. No key is derived from `SECRET_KEY`. A lost key makes its ciphertext unrecoverable; replacing key bytes under an existing identifier does not rotate old ciphertext. New key material requires a new identifier. The application checks key length/encoding and identifiers; the deployment security check reports configuration failures without printing material.

This application change does not provision keys, alter a service environment, configure a secret manager or change any deployment files. Bootstrap-only Django settings still come from externally managed configuration; saving a runtime preference does not rewrite that configuration or restart a process.

## Existing-data conversion

Coordinate conversion with an application maintenance window. The new reader deliberately rejects legacy plaintext, so complete key provisioning and conversion before enabling normal traffic against an existing database. Keep a controlled backup and the required external keys together in the recovery plan without storing the keys in the database backup itself.

Run with the intended environment and database explicitly selected:

```powershell
.venv/Scripts/python.exe manage.py encrypt_integration_settings
.venv/Scripts/python.exe manage.py encrypt_integration_settings --apply --batch-size 100
.venv/Scripts/python.exe manage.py encrypt_integration_settings --check
```

The first command only inspects. The apply command locks and converts each legacy row in its own transaction and verifies the reread ciphertext. Batch size bounds identifier retrieval, not an all-or-nothing transaction. It prints counts, never values. Interruptions leave earlier verified rows committed; rerun the same command to resume. An unreadable encrypted row stops conversion with its row ID and does not overwrite that row. Investigate its key/recovery source rather than substituting a default credential.

The check must succeed before declaring the current database converted. Also verify intended integrations against controlled provider test accounts, ordinary staff denials, owner confirmation, and recovery from a representative protected backup. Conversion was tested on isolated databases during development; it has not been applied to the existing customer database.

## Key rotation

1. Add a new random key under a new identifier while retaining old identifiers.
2. Set the active identifier to the new key. Verify reads and new encrypted writes.
3. Inspect, then run `encrypt_integration_settings --apply --rotate`.
4. Run `encrypt_integration_settings --check` to verify no legacy or old-key rows remain in the current database.
5. Retain old keys for the documented recovery/backup-retention period. Remove them only when every retained snapshot that requires them has been handled under the recovery policy.

Changing application code back to a plaintext-only reader is not a valid rollback. Recovery must retain a reader and keys compatible with the stored envelopes. The password-encrypted configuration export is portable across storage keys because import decrypts the archive and encrypts rows for the destination key; protect its password separately.

## Remaining external actions

Current-row encryption does not erase historical plaintext from database snapshots, journals, backups, previous `.env` copies, cache snapshots, logs or provider systems. Inventory and handle those copies under the applicable retention/recovery policy, and rotate provider credentials that were stored in plaintext. Applying the conversion removes known old runtime-cache keys but does not rewrite archived cache data. Separate key provisioning, provider rotation and historical cleanup are prerequisites outside this application's implementation scope. The historical finding is not closed by code alone.
