# Roadmap implementation status

This document tracks the full scope of [application-roadmap.md](application-roadmap.md). A row is complete only when the implementation and its acceptance checks have been verified. Swarm and deployment changes remain excluded.

## Security workstreams

| Workstream | Status | Evidence |
|---|---|---|
| SEC-01 — Authentication and MFA | Verified | 37 focused security tests; existing account tests; 432 application tests pass (one pre-existing nginx-port assertion fails); Django checks and accounts migration drift check pass; four desktop/mobile browser accessibility states pass. See implementation notes below. |
| SEC-02 — Staff permissions | Verified | Explicit fail-closed route policies, six operational roles, owner-only security controls, recent-MFA confirmation, audited role changes and bounded impersonation. 309 authorization tests; full suite 741 passed with one unchanged nginx baseline failure; 16 desktop/mobile browser states pass. See authorization design and verification below. |
| SEC-03 — Application dependencies | Verified with documented unpatched PDF dependency exception | Updated direct dependencies and tested matching production/development resolutions. 911 tests pass; 24 UI states pass; pip/Django checks pass. Complete profile scans have one visible high-severity pdfkit advisory with no patched upstream release; see dependency-updates.md and saved audit reports. |
| SEC-04 — Trusted client IP | Application verified; external boundary verification outstanding | One validated socket-aware parser, explicit trusted CIDRs defaulting to none, bounded forwarding-chain parsing, normalized allowlists and shared security consumers. 39 focused tests; full suite 788 passed with unchanged nginx baseline failure. See trusted-client-ip.md for external prerequisites. |
| SEC-05 — Encrypted integration secrets | Application verified; external key provisioning and existing-data conversion outstanding | Versioned AES-256-GCM storage for all integration rows, identity-bound authentication, no plaintext runtime cache or HTTP .env writes, transactional field-name audit, resumable verified conversion/rotation and safe failure responses. 25 focused encryption tests pass, including registrar-widget redaction. See encrypted-settings.md and latest SEC-08 regression evidence. |
| SEC-06 - Public endpoint abuse controls | Application verified; external key/cache/worker runtime validation outstanding | Early shared limits, typed validation, durable receipts/outbox, encrypted expiring account mail, bounded domain/PDF work and accessible recovery states. 153 focused security tests across the public suites; full suite 1,064 passed with the unchanged nginx baseline failure; 52 browser states pass. See completion audit below and account-mail-delivery.md. |
| SEC-07 — Safe content and uploads | Application verified; historical media review/external origin isolation outstanding | Inert catalogue JSON, escaped SVG text, bounded raster decoding/re-encoding enforced at storage and form boundaries, and intersected response CSP policies. 22 focused tests; full suite 810 passed with unchanged nginx baseline failure; 20 desktop/mobile UI states pass. |
| SEC-08 — Bounded configuration imports | Verified | Bounded uploads/archive expansion, exact manifest/member structure, duplicate rejection, explicit typed setting registry, destination-key encryption and transactional import/audit rollback. 74 focused import tests pass; representative desktop/mobile restore screens verified. See configuration-backups.md. |
| SEC-09 — Health disclosure | Verified | Generic status values, server-generated correlation IDs, private exception logging, actual database query and unique cache round-trip probes. Eight focused health tests pass; full suite 749 passed with the unchanged nginx baseline failure. |

## Product improvements

| Item | Status | Evidence |
|---|---|---|
| 1 - Admin components and accessibility | Verified | Reusable page/action/card/table/form/status/loading/pagination components adopted in representative workflows; modal drawer focus and mobile scroll handling verified. Seven component tests, 1,071 whole-suite passes and 64 browser states. See admin-components.md and completion audit below. |
| 2 — Permission-aware global search | Pending | |
| 3 — Customer 360 workspace | Pending | |
| 4 — Saved list views | Pending | |
| 5 — Bulk domain operations | Pending | |
| 6 — Renewal and payment-readiness centre | Pending | |
| 7 — Domain transfer workflow | Pending | |
| 8 — DNS preview, history, rollback | Pending | |
| 9 — DNS presets | Pending | |
| 10 — Order and provisioning timeline | Pending | |
| 11 — Integration exceptions and reconciliation | Pending | |
| 12 — Job progress and safe retry | Pending | |
| 13 — Quote lifecycle | Pending | |
| 14 — Billing corrections and reconciliation | Pending | |
| 15 — Support agent workspace | Pending | |
| 16 — Knowledge base | Pending | |
| 17 — Customer team access | Pending | |
| 18 — Account security centre | Pending | |
| 19 — Notification inbox | Pending | |
| 20 — Scoped API and event subscriptions | Pending | |

## SEC-01 implementation and verification

- Normal and test settings share one MFA-aware authentication backend. A system check rejects a password-only fallback backend.
- Password validation produces an anonymous, database-backed challenge that expires after five minutes and is bound to the current password/MFA state and browser session. Resubmitting a password invalidates that browser's previous challenge.
- Challenge and account attempt limits persist across application workers and browser sessions. Empty/malformed submissions count; a successful factor resets the account failure window.
- TOTP consumption uses a database compare-and-set on the accepted time step. Backup-code consumption uses a conditional unused-row update. Database transactions and user-row locks coordinate factor/challenge operations.
- Django admin uses the portal sign-in flow. Allauth initial, resumed, direct-adapter, and email-confirmation flows require MFA before establishing a session. No social provider is configured in this repository; the shared allauth login boundary is covered, not a live third-party OAuth exchange.
- Session hashes include versioned security state. Existing sessions from before the fix become invalid when the updated application is used; changes to MFA/activation rotate session state so toggling it cannot resurrect an old session. Enrollment and disable preserve the acting session through Django's session-hash update.
- Login/challenge and backup-code pages prohibit caching. The MFA screen has explicit labels/error associations, readable colours, no automatic example/card injection, and keyboard navigation checks.
- Added accounts migration `0003_mfa_challenges` and reproducible browser checker `scripts/verify_authentication_ui.py`. Tests apply the migration in an isolated database; an existing customer database was not migrated during development.

Verification commands:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_security_authentication.py -q -p no:cacheprovider
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
.venv/Scripts/python.exe manage.py check --settings=cyberask_domains.settings.test
.venv/Scripts/python.exe manage.py makemigrations accounts --check --dry-run --settings=cyberask_domains.settings.test
.venv/Scripts/python.exe scripts/verify_authentication_ui.py --axe "$env:TEMP/admin-theme-axe.min.js"
```

The browser check uses a locally available axe-core bundle and installed Chrome, renders anonymous MFA and invalid-code states at widths 1440 and 390, and records screenshots/results under the system temporary directory. No live accounts or provider services are contacted.

The full-suite baseline failure is `tests/test_nginx_site_config.py::test_portal_default_route_is_configured_separately_from_site_includes`: the test expects port 80; the unchanged nginx configuration uses port 780. Deployment files remain outside this roadmap implementation scope. Separate pre-existing migration drift exists in admin_tools, domains, support, and website_templates; accounts itself has no migration drift.

## SEC-02 implementation and verification

- [Staff authorization design](staff-authorization.md) documents the role matrix, route policies, confirmation boundaries, impersonation rules and migration behaviour.
- Every Admin Tools route has an explicit policy. Unknown routes fail closed; a Django system check reports inventory omissions. Method-aware checks run before forms, object lookup and provider work. Mixed domain/billing/hosting forms require each relevant capability.
- Six predefined groups receive narrowly scoped custom permissions through migration `admin_tools.0003_staff_roles`. Existing staff receive no automatic group assignment. An active staff superuser is a platform owner; ordinary staff cannot become owners through operational permissions or native-admin model permissions.
- Security/settings/integrations/secret-export/allowlist/account-management routes require an owner and MFA proof from the previous five minutes. Native Django admin has the same gate. Confirmation checks the current password and a fresh factor through shared account throttling and replay prevention. Expired POSTs are denied without replay.
- Owner role assignments require a reason and mandatory transactional audit, preserve unrelated groups, and revoke the affected staff member's existing sessions. Own privilege changes and own administrative MFA resets are rejected.
- Impersonation requires a recently verified owner and support reason, permits active customers only, expires after 30 minutes, and rechecks the owner's current security state on every request. Credential, API-key, export and saved-payment-method operations are blocked, including alternate allauth account-management routes. Restoring the owner requires a valid context and fresh confirmation before another sensitive action.
- Navigation and the staff landing page follow permissions; customer lists omit privileged accounts and owner actions for operational staff. Delegated content/domain/hosting forms protect ownership, provisioning identity and financial configuration. Admin responses use `Cache-Control: no-store`.
- `tests/test_staff_authorization.py` covers the entire route inventory for unassigned staff, all six roles, denied side effects, cross-capability operations, revocation, malformed/stale identity proof, CSRF, audit failure, owner self-protection and impersonation boundaries. Earlier functional tests now authenticate verified owners explicitly where required.

Verification: full suite **741 passed, one unchanged nginx baseline failure**; Django system checks clean; accounts migration drift check clean; `git diff --check` clean. Existing admin_tools WizardProgress metadata drift remains separate from the new role migration. Migrations were exercised on isolated test databases, not an existing customer database.

The reproducible browser checker now renders eight states at widths 1440 and 390: MFA, invalid MFA, identity confirmation, expired confirmation, staff roles, invalid role assignment, staff workspace and staff customers. All 16 pass automated scoped accessibility and overflow checks; keyboard checks and screenshots cover the authentication forms. Mobile role management and expired-confirmation screenshots were also visually reviewed. This verifies these new screens, not every admin screen or all of product item 1.

## SEC-09 implementation and verification

`GET /health/` retains `status`, `database` and `cache`, and HTTP 200/503 semantics. Component failures now return only `error`; overall status is `degraded`. An additional server-generated `correlation_id` appears in JSON and `X-Correlation-ID`. Client-supplied correlation headers are not trusted. Responses prohibit caching; HEAD is supported and mutating methods return 405 before probes run.

Database health executes and verifies `SELECT 1`, including on already-open connections. Cache health writes and reads a unique short-lived value, verifies equality, and deletes the key; concurrent probes cannot hide a failed write behind an old value. Backend timeouts remain governed by the configured database/cache clients; no server or deployment configuration changed.

Failure details and tracebacks are logged internally with component and correlation ID, never serialized to the public response. Treat those internal logs as restricted operational data. Tests simulate database/cache exceptions containing credentials and internal hostnames and verify their absence from response bodies/headers, correlation to real log records, simultaneous component failures, silent cache write failure, unexpected query results, unique cache keys, cleanup, HEAD and method restrictions.

Verification: `tests/test_health_security.py` and subdomain-routing tests **15 passed**; full suite **749 passed, one unchanged nginx baseline failure**. SEC-09 requires no migration or visual screen.

## SEC-04 application implementation and verification

`apps/core/http.py` is the only application reader of `REMOTE_ADDR` and `X-Forwarded-For`. It validates and canonicalizes IPv4/IPv6, normalizes IPv4-mapped addresses, rejects scoped/port-bearing addresses, ignores all forwarding headers for untrusted peers, and walks explicitly trusted forwarding chains right-to-left. Malformed chains from trusted peers fail closed to an unknown address. Length and hop limits bound parsing.

Login/domain throttles, quote capture, contact submissions, audit records and staff allowlists share this result. Unknown addresses use the existing shared unknown throttle bucket, store NULL in nullable address fields and cannot match an allowlist. Stored allowlist addresses use the same normalization. Django check `core.E001` reports invalid trust configuration.

`tests/test_client_ip_security.py` contains 39 cases for direct spoofing, trusted/untrusted multi-hop boundaries, IPv4/IPv6/mapped normalization, malformed/oversized chains, invalid configuration, consistent throttle consumers, real allowlist denials, audit persistence and contact submission capture. Full suite: **788 passed, one unchanged nginx baseline failure**; Django system checks and diff whitespace checks pass. No migration or visual screen is involved.

[Trusted client-IP contract](trusted-client-ip.md) documents example chains and external validation requirements. No proxy is trusted by default. Actual proxy header handling, network reachability and the correct socket-peer configuration must be verified outside this application work; SR-005 is not claimed closed at the infrastructure boundary.

## SEC-07 application implementation and verification

- The public quote builder uses Django `json_script` with the Python catalogue object. Script terminators, ampersands and other special characters round-trip as data without generating executable elements. No pre-serialized `|safe` JSON remains in this path.
- Generated website thumbnails escape both title and initials as XML text and remove XML-invalid code points. Their response has a restrictive sandbox policy and `nosniff`.
- Branding accepts static PNG, JPEG and WebP input only. Content is decoded independently of the supplied extension/MIME type, verified, and re-encoded as a newly generated PNG filename. Re-encoding strips metadata, profiles, trailing active data and the original filename; valid raster bytes with a misleading extension therefore become a safe PNG. SVG/HTML, truncated/unsupported/animated input and decoder bomb warnings are rejected.
- Input reads are bounded to 5 MB, each dimension to 4096 pixels, total pixels to 8 million, and encoded output to 8 MB. These are application processing limits, not a claim that the network upload was bounded at the proxy.
- `SafeLogoField`/`SafeLogoFile` enforce sanitization at file persistence, including direct FieldFile.save calls. The model validator provides native-admin form errors; the Admin Tools handler validates before any branding mutation. Upload-and-remove conflicts are rejected without deleting the existing logo. Migration `billing.0007_safe_branding_logo` records the custom field and validator; it does not rewrite historical media.
- Branding UI lists accepted formats/limits, associates the input with help/errors, and links the error summary to the field. Invalid uploads do not partially save unrelated branding changes. Mobile invalid-upload rendering was visually inspected.
- CSP middleware appends the configured policy as an independent comma-separated policy, so browser enforcement intersects it with the endpoint's existing policy. Endpoint sandbox and restrictive script directives are preserved rather than overwritten. This may make a preview more restrictive; it never relaxes an endpoint policy to make content run.

Verification: **22 content/upload security tests**, including real PNG/JPEG/WebP decoding, XML parsing of injection payloads, JSON round trips, storage persistence, model-form validation, rejection without partial changes, dimension/input/output limits and CSP composition. Full suite **810 passed, one unchanged nginx baseline failure**. Django system checks clean; billing migration drift check clean; diff whitespace checks clean. Tests exercise the migration in isolated databases only.

The browser checker now includes normal and invalid branding forms at widths 1440 and 390, for **20 passing states** in total. Automated checks cover scoped colour contrast, names/labels, relevant ARIA values and document overflow, alongside the prior authentication keyboard checks. These are representative states, not an application-wide accessibility certification.

**Remaining boundary:** Existing files uploaded before this change are not automatically rewritten or deleted and may still have historical URLs. Inventory those objects and historical copies, re-upload valid logos through the sanitizer and separately remove/quarantine unsafe historical media under the storage retention policy. Separate media origin/serving configuration remains excluded. SR-008's infrastructure-dependent media-origin risk is not claimed closed. Dependency patching, including the image decoder version, remains tracked independently under SEC-03.

## SEC-05 application implementation and verification

- All `IntegrationSetting` values are encrypted at write, including public settings and incorrectly labelled secrets. `is_secret=False` cannot accidentally store a usable credential in plaintext. Storage uses a fresh 96-bit nonce, AES-256-GCM, a versioned key identifier and authenticated setting identity. Moving a ciphertext to a different setting or altering bytes fails authentication.
- Encryption keys are provided separately through application settings, never derived from the Django secret or saved in the database. The normal configuration has no built-in key. Isolated test settings have an explicitly public test-only fixture key. Deployment security check `admin_tools.E003` reports missing/invalid key configuration; runtime writes fail safely without it.
- Runtime consumers decrypt explicitly. Missing rows can use the environment/default; existing blank rows remain explicitly blank. Legacy, corrupt, unavailable-key and authentication failures never silently fall back. Ready-application database outages propagate rather than selecting another credential. HTTP encryption failures return a generic non-cacheable 503 and server-generated reference without cryptographic material.
- Runtime reads no longer use the shared plaintext cache. Standard writes and bulk-write guards prevent ordinary ORM paths from bypassing encryption; settings maintenance retains an explicit privileged raw-storage conversion path. Native admin presents protected read-only settings rather than plaintext or editable ciphertext.
- The setup wizard no longer edits or rereads `.env` during requests. It reads the common runtime source and stores encrypted database values. Multi-setting wizard and Graph changes are transactional. Normal setting changes record actor/key names and creation state in mandatory audit entries without values; audit failure rolls back the write.
- Password-encrypted backup export reads authenticated plaintext only in memory, while import re-encrypts it using the destination's active storage key. Existing owner/recent-MFA route gates cover integration changes and import/export. Bounds/schema validation of backup import remains separately tracked under SEC-08.
- `encrypt_integration_settings` performs read-only inspection by default; `--apply` converts existing plaintext and `--apply --rotate` rewraps previous key versions. Each locked row is decrypted/verified after persistence; completed rows survive interruption and repeated runs skip current ciphertext. `--check` fails if conversion/rotation remains incomplete. Key material, plaintext and ciphertext are never printed. The command also removes known legacy runtime-cache entries when applying.

Verification: **24 encryption tests pass**, covering raw database reads, incorrect secret flags, nonces, cross-setting substitution, tampering, missing/invalid keys, stale plaintext cache, explicit blanks, database outage, ORM guards, mandatory audit rollback, conversion dry-run/idempotency/resumption, old-key rotation/retirement, password-backup portability and generic HTTP failure. One test creates a separate temporary database and launches fresh management-command processes to verify conversion can bootstrap from legacy storage. The last full-suite run before adding that isolated command test was **833 passed, one unchanged nginx baseline failure**. Django system checks pass; no new schema migration is required for the existing text storage. Pre-existing WizardProgress Meta migration drift remains separate.

[Encrypted settings operations](encrypted-settings.md) describes conversion, key rotation, validation and remaining prerequisites. No existing customer database, production key, provider credential or `.env` was changed during development. Actual external-key provisioning, existing-data conversion, provider credential rotation and historical backup/cache/file cleanup are outstanding operational actions. SR-006 is not claimed closed for historical copies or provider credentials.

## SEC-08 implementation and verification

- Uploads and direct import arguments are bounded to 2 MB. ZIPs must contain exactly README.txt, manifest.json and payload.bin, without duplicates, path variants, directories, symlinks, encrypted ZIP entries or unsupported compression. Each member has a byte limit; total expansion and a 50:1 compression ratio are bounded before decompression. Actual member reads are also bounded and checked against declared sizes; corrupt CRC/deflate data is rejected.
- The fixed manifest specifies the supported version, AES-256-GCM and fixed PBKDF2 parameters. It cannot choose a different algorithm or KDF cost. Passwords are bounded to 8–1024 characters. Existing correctly formed v1 exports remain supported.
- JSON parsing rejects duplicate keys, invalid encoding, excessive nesting and non-finite constants. Root, timestamp, row structures/counts and flags are typed, with no coercion of arbitrary objects into strings or booleans. Setting rows cannot repeat.
- `setting_schema.py` explicitly enumerates supported application settings and their types/lengths. Ports, booleans, timeouts, mailboxes, hostnames, HTTPS endpoints, identifiers, timezones, slugs, nameserver lists and monetary thresholds have appropriate validation. Process credentials, database/Redis URLs, proxy trust, encryption keys and unknown feature flags cannot be imported. Secret flags are derived from the registry rather than trusted from the archive; all values are encrypted regardless.
- Every row is validated before writes begin. Import writes and mandatory audit entries share one outer transaction; simulated failures after an earlier update leave the original values and audit state intact. Restores use the destination active encryption key and record the verified owner as actor. Existing owner/recent-MFA access gates run before the handler.
- Exports use the same schema so unsupported/invalid current rows cause an explicit error instead of generating an archive the application cannot restore. The import UI explains the 2 MB limit, supported-setting scope, replacement semantics and all-or-nothing behaviour; password inputs reflect server length limits.

Verification: **74 focused import tests pass**, covering actual password encryption/decryption, row validation, archive limits/structure/ratios, duplicate ZIP/JSON/setting keys, invalid UTF-8/depth/NaN, malformed manifests, symlinks, unsupported compression, CRC damage, tampered ciphertext, dishonest upload-size metadata, atomic rollback, canonical secret flags, actor audit and expired-MFA denials. The registrar widget redaction regression also passes, taking SEC-05's focused encryption coverage to **25 tests**.

The browser checker includes normal settings and invalid-import feedback at widths 1440 and 390, for **24 passing states** overall. The invalid mobile settings page was visually reviewed; all states pass scoped accessibility and overflow checks. [Backup format and restore contract](configuration-backups.md) documents limits, compatibility and exclusions. No customer configuration was imported/exported during development; tests use isolated fixture values only.

Final regression result for this workstream: **909 passed, one unchanged nginx-port baseline failure**. Django system checks and diff whitespace checks pass. No schema migration is needed for bounded imports. This does not close the external prerequisites recorded for SEC-04, SEC-05 or SEC-07, or complete the remaining security/product roadmap items.

## SEC-03 dependency upgrades and verification

Direct production/development pins were updated from current PyPI metadata and upstream release/security guidance, retaining Django's supported 5.2 LTS series at 5.2.18. The tested versions, compatibility adjustments, sources and PDF exception are recorded in [dependency updates](dependency-updates.md). No container, OS, server-process or deployment configuration changed.

Allauth email-verification callers now use the installed release's verification flow and the current email-only login/signup settings. Existing verification, password reset, MFA and alternate-session tests pass. Stripe calls use one configuration function that explicitly retains the original `2023-10-16` API contract. Real SDK requests intercepted at the HTTP layer verify refund parameters, customer creation, setup intents and the wire-version header; no live provider was contacted. The customer path's unsupported get_full_name call was corrected to the actual full_name property. Health-log tests count emitted records rather than duplicate logging routes under the updated test stack.

The baseline direct-pin scan recorded **87 unique advisory-ID/package pairs across nine packages** (the raw service returned some duplicate records). Fresh complete dependency resolutions were audited: **57 production packages** and **88 development packages**, with **no skipped packages**. Both have exactly **one remaining unique advisory**, for pdfkit 1.0.0. Both resolved sets match the installed versions used for testing. The broader 96-package local environment scan has the same remaining finding. This is not a zero-vulnerability claim.

`scripts/audit_dependencies.py` resolves a complete profile without installing it, scans explicit resolved pins (including transitives), saves normalized reports and pins, and exits unsuccessfully for findings or skipped packages. It does not suppress the documented PDF exception. [Saved audit evidence](dependency-audit/) includes the baseline direct-only report and complete post-upgrade profile reports. The baseline and final scopes differ and are labelled accordingly.

Verification: full suite **911 passed, one unchanged nginx-port baseline failure**; the representative desktop/mobile checker **24 states passed**; `pip check`, Django system checks and diff whitespace checks pass. Windows/Python 3.13 application tests and isolated SQLite migrations were exercised. PostgreSQL, Linux worker/server startup and live provider round trips were not run; this work does not claim deployment verification.

The unpatched PDF advisory remains a documented high-severity exception, not an inferred user acceptance or a finding closed by the upgrades. It needs renderer replacement and runtime validation. SEC-03's acceptance criteria permit a documented exception; other security/product work remains tracked separately.

## SEC-06 work in progress — request boundary and typed validation

The previous goal turn implemented and verified SEC-03's dependency updates. This turn adds the following concrete SEC-06 foundations; it does **not** close SEC-06 or advance to another product item:

- `apps/core/abuse.py` inventories public contact/quote submission, custom/allauth registration and login, password reset, domain search/check and WHOIS. Its process_view boundary precedes CSRF body parsing, uses the common verified client IP and limits declared/visible bodies to 32 KB for forms or 64 KB for quote JSON. Query strings are bounded to 2048 characters. Unsupported content types/files, malformed JSON/encoding/duplicate keys and oversized authentication passwords are rejected before view work.
- Fixed-window counters use shared-cache atomic add/incr, with no sliding read/modify/write race. They include per-IP, normalized-email, authenticated-account and global endpoint budgets. Both signup paths and both login URLs share counter families. Identities are HMAC-hashed in cache keys. Cache lookup/counter failure denies work with generic 503; quotas return 429 and Retry-After. Deployment check core.E002 requires a shared Redis cache; no real service/cache configuration changed.
- Current policy budgets: contact/quote 5 per IP and 3 per email per 600 seconds; signup the same; password reset 10 per IP and 3 per email per 600 seconds; login 60 per IP and 10 per email per 300 seconds in addition to existing failure controls; domain check/search 20 per IP/minute; WHOIS 10 per IP/minute. Authenticated-account budgets are twice each endpoint's IP budget. Separate per-minute endpoint ceilings bound aggregate work. These are initial application defaults to validate/tune during completion, not provider-capacity guarantees.
- Public quotes now have typed lead fields, at most 20 catalogue lines, whole quantities 1–100, bounded descriptions and contact text, valid active/quotable package IDs and a maximum gross value of 100,000 including tax. One bounded catalogue query supplies prices. Invalid lines reject the submission rather than being silently dropped. Arbitrary 'setup' text cannot select the cheaper setup price; explicit setup lines or the old catalogue's exact setup label are handled separately. Repeated package/price-kind lines are rejected.
- Contact messages are limited to 10,000 characters and honeypot text to 200. All user fields in HTML staff email are escaped, including the mailto attribute. Registration passwords are bounded before hashing. Domain search is explicitly GET-only.
- The test configuration uses a separate isolated abuse cache. An autouse fixture resets only that test cache and refuses to clear a shared cache. Controls remain enabled during tests. Concurrency tests exercise the atomic counter API; real multi-process Redis/boundary behaviour is still an external verification scope.

Current evidence: **42 focused abuse tests pass**; abuse + billing checks **57 passed**. They cover concurrent quota admission, fixed-window rollover, private cache keys, cross-IP email limits, signup-path sharing, cache outages, early declared/visible body bounds, malformed JSON, typed quantities/fields, catalogue pricing, gross-value caps, contact HTML escaping and the shared-cache system check. The last full suite before the final password-length check was **952 passed, one unchanged nginx-port baseline failure**. Django system and diff whitespace checks pass. No schema migration is involved in this foundation.

**Outstanding at the initial request-boundary checkpoint:** durable scoped idempotency, queued email/PDF execution, domain fan-out bounds and complete public error/retry UI. The following outbox checkpoint supersedes the earlier synchronous contact/quote delivery path. No external mail/provider action or customer database mutation was performed during tests.

## SEC-06 work in progress — durable receipts and delivery recovery

- Added PublicSubmission, PublicDelivery and PublicDeliveryGate through core migrations 0006–0008. Unique receipt keys and one-to-one delivery rows fence document/job creation within one transaction. Contact/quote views now persist work rather than rendering PDFs or sending mail. Browser forms supply UUID keys and establish their owner marker on GET; responses with submission keys/results prohibit caching.
- Explicit UUID receipt identity survives Django signing-key rotation, while owner/payload bindings remain HMAC-protected. Repeated keys return the original result only for the same owner and data; changes or another browser receive 409 without disclosure. Legacy requests without a key deduplicate identical data within the same session. A fresh form/key denotes an intentional new submission. SQLite writer contention returns a generic safe retry after rollback.
- A locked admission row caps pending/preparing/delivering/failed/uncertain jobs at 1000 before new contact/quote work is created. Database-only contacts do not need delivery capacity. Broker publish failures retain queued rows; the application dispatcher republishes at most 100 due jobs per tick. Eager Celery configuration cannot run public delivery in an HTTP callback; the job remains queued and deployment check core.E003 reports the configuration problem.
- Workers claim with per-attempt lease tokens. Preparation is retried at most three times with backoff. A lost preparation lease cannot cross the send boundary. The delivering state is persisted before the transport call; exceptions, missing acknowledgment or stale sending state become unknown and are not automatically resent. Stale preparation is safely reclaimed. SMTP timeout is 15 seconds; public Graph request timeout is capped at 10 seconds and public messages prohibit Graph-to-SMTP fallback after uncertainty. Task soft/hard limits are declared; renderer subprocess bounds are described in the later checkpoint.
- Successful/suppressed/closed jobs discard their delivery payload. Email-only contacts create no ContactSubmission row. Sent mail uses a stable Message-ID for provider correlation. Job errors/log messages contain generic codes/job references rather than submitted message bodies or provider exception text.
- Existing owner/recent-MFA Task Management now lists review/queued/active work and provides reasoned, audited actions: retry known preparation failure, confirm an uncertain delivery from provider evidence, or close without sending. It refuses blind uncertain-send retries and active-send cancellation. Manual preparation retry preserves lifetime attempt totals. Review/audit updates are transactional. The header navigation was moved into a wrapping content toolbar so the mobile title stays visible.

Verification: **26 delivery tests and 42 abuse tests pass**. Coverage includes replay ownership/payload conflicts, signing-key rotation, no-key session replay, concurrent receipts with safe SQLite retries, atomic creation rollback, broker failure recovery, eager-execution refusal, bounded preparation retries, uncertain/no-repeat sends, stale states, lost leases, capacity admission, email-only privacy, owner review/audit rollback and Graph no-fallback/timeouts. Full suite: **979 passed, one unchanged nginx-port baseline failure**. Django checks, core migration drift and whitespace checks pass. Migrations and callbacks were exercised on isolated test databases; no existing customer database was migrated.

The browser checker now includes populated delivery review and invalid-review states at widths 1440 and 390, for **28 passing states** overall. Automated scoped accessibility/overflow checks pass; the invalid mobile screen was visually inspected after fixing the crowded header.

## SEC-06 work in progress — domain query and fan-out bounds

- Availability rejects names longer than 253 characters and malformed/unsupported typed extensions before registrar work. Explicit extension filters are limited to 512 characters and ten entries, validate against supported extensions and deduplicate entries. Default checks select at most ten supported extensions; a full supported domain can select an extension beyond that first batch.
- Search-page missing-price synchronization also selects at most ten extensions. Search/result templates explain the first-batch limit when the configured list is larger. Invalid configured extension shapes are excluded from the public selection.
- The public route now relies on the shared atomic request boundary rather than its former non-atomic cache read/modify/write counter. Missing-price synchronization uses atomic cache admission with a bounded hashed key and failure cooldown, preventing concurrent callers from starting the same synchronization.
- Live debug cache bypass/pricing now requires an active platform owner with recent MFA, in addition to the global flag. Anonymous callers, ordinary staff and expired owner proofs cannot force live debug work. Provider-constructor failure produces unknown availability rather than referencing an uninitialized client.
- WHOIS rejects invalid labels and oversized names before outbound requests. This bounds user-controlled input and request cardinality; it does not establish a hard wall-clock limit against a hostile upstream HTTP stream.

Focused verification: **19 domain-bound tests plus 11 pricing tests passed**. Coverage includes default fan-out, full-name selection beyond the first batch, unsupported/malformed/oversized filters, bounded search-page sync, concurrent sync admission, anonymous/staff/expired-owner debug denial, provider initialization failure and WHOIS early rejection. No live registrar or RDAP endpoint was contacted by these tests. Public error/retry presentation and native PDF execution limits are still required before closing SEC-06.

Full regression after this checkpoint: **998 passed, one unchanged nginx-port baseline failure**. Django system checks and whitespace checks pass. No migration or deployment change was required for domain input/fan-out bounds.

[Public delivery contract](public-delivery.md) documents these states and limits. **Still required before SEC-06 is verified:** final alternate public-mail/search inventory checks; final feature-wide regression/design checks. Public error/retry presentation and form-limit guidance are covered by the later checkpoint. Domain query/fan-out bounds are now covered by the checkpoint above. Actual Redis/PostgreSQL multi-worker and live mail/broker/runtime validation remain external test scopes, not claims established by isolated fixtures. The unpatched pdfkit advisory remains the separately documented SEC-03 exception. The 20 product improvements remain in scope and have not been substituted with this workstream.


## SEC-06 checkpoint: bounded PDF execution and credential completion

PDF engines run through `apps/core/process_limits.py` without a shell and with an explicit environment allowlist that omits application credentials. Windows uses a kill-on-close Job Object; POSIX uses a separate process group. Timeout, output overflow, interruption and normal completion clean up descendants. These controls bound execution and captured output; they are not a security sandbox or a child-memory limit.

The renderer accepts at most 1 MiB of combined document/header/footer HTML, captures at most 10 MiB of PDF and 64 KiB of diagnostic output, and shares one 15-second execution deadline across optional engines. OS process creation and bounded cleanup can add wall-clock overhead. Invalid PDF signatures are rejected. Timeout/overflow stops the engine chain and preserves the existing HTML fallback. Oversized template input fails preparation before a renderer starts. The standalone WeasyPrint worker receives a bounded JSON packet, without Django setup or database access. Random PDFKit metadata prefixes prevent document metadata from supplying wrapper options.

Public quote PDF downloads allow five requests per client per ten minutes (global 30/minute); authenticated invoice downloads allow ten (global 60/minute), with an additional account counter. Both require GET and prohibit response caching. Reset-token completion, optional reset-code completion, MFA verification and identity confirmation now have early 32 KiB form bounds and rate limits. Reset-completion aliases share a rate family; existing account/challenge attempt controls remain in force.

Verification: eight real subprocess tests and nine renderer/download tests pass on Windows, including timeout/descendant termination, output floods, credential environment isolation, interruption cleanup, malformed worker protocol and download limits. The abuse suite now has 46 passing tests; all three focused suites total 63. Renderer adapter tests use controlled engine fixtures: native wkhtmltopdf/WeasyPrint rendering, font/layout output and POSIX execution have not been verified here. The known unpatched pdfkit advisory remains open; resource bounds do not remediate local-file/JavaScript exposure. Accessible public error/retry presentation and final inventory/regression checks remain before SEC-06 completion.

Final checkpoint regression: **1,019 tests passed**; the one unchanged failure is `test_portal_default_route_is_configured_separately_from_site_includes`, which expects nginx port 80 while the existing configuration uses 780. Deployment changes remain excluded. Django system checks and `git diff --check` pass. The existing database-access-during-initialization warning remains. No customer database, deployment or live provider was changed.


## SEC-06 checkpoint: public recovery screens and form guidance

- Browser document requests receive an escaped, database-independent recovery page for boundary failures. JSON quote clients retain their machine-readable contract. HTMX failures carry an explicit marker and render only text nodes in the existing results area; they do not inject response HTML, clear the query or move focus during live search. Negotiated responses vary on Accept/HX-Request and prohibit caching. Rate failures expose consistent Retry-After timing and JSON retry_after guidance.
- Recovery pages explain waiting, shortening oversized submissions and using browser Back to recover details. They never echo submitted bodies or passwords. No automatic POST resubmission is introduced.
- Contact field errors use Django's error IDs/ARIA associations, stronger colours and a visible message limit. Quote contact fields have explicit labels, maximum lengths and line/quantity guidance. Quote rate failures retain cart/contact state and the same submission key. Re-adding a removed monthly line does not duplicate its setup line. Domain inputs have explicit names and 253-character limits; the extension guidance reflects the ten-extension bound.
- Browser checks uncovered and corrected low-contrast public contact/search/quote buttons, quote category text and the shared cookie acceptance button. These are verified public states, not a claim that every application page has passed the upcoming component-library workstream.

Verification: eight public error-contract tests pass, including zero database queries during recovery rendering, escaping, content negotiation, retry headers and field-error associations. The focused public suites total **99 passing tests**. The full regression has **1,027 passing tests**, with the unchanged nginx port-80/780 baseline failure. Subsequent template/colour/cart refinements pass the expanded **42 desktop/mobile browser states**, including actual add/remove/re-add quote controls, simulated HTTP 429 responses, retained inputs, keyboard recovery links, contrast/name checks and no document overflow. Mobile quote and recovery screenshots were visually inspected. The fixture browser does not replay production CSP headers or contact a live provider. Django checks and whitespace checks pass; the existing initialization warning remains.

SEC-06 remains in progress. The final inventory identified that `quote_public_accept_continue` currently changes acceptance state on a post-login GET. This issue is addressed in the subsequent acceptance checkpoint below. Complete the remaining public-mail inventory before closing this workstream. The 20 product improvements remain pending and in scope.


## SEC-06 checkpoint: quote acceptance confirmation and queued invoices

The post-login continuation GET now renders a read-only, account-labelled confirmation page. It does not link the account, stamp acceptance or create an invoice. Both acceptance routes require CSRF-protected POST to finalize; unsupported methods are denied. Public quote detail is GET-only and no-store. Acceptance responses/confirmation/recovery prohibit caching, and overload responses carry Retry-After. The two POST routes share one early 32 KiB/rate budget.

`apps/billing/acceptance.py` owns one transaction covering the ownership check, acceptable/expiry checks, conditional account/status claim, conversion, unique receipt and durable invoice-delivery job. The quote row is locked; the conditional write provides a fail-safe contention boundary for SQLite. Database contention is a generic retryable failure after rollback. The general conversion service reloads under a quote lock, so a stale caller cannot allocate another invoice from an earlier model instance. Replay from the same account across sessions returns the existing invoice before creating any work; a foreign account is denied before invoice disclosure.

Public acceptance calls conversion with email disabled, then creates one invoice-kind outbox job in the same transaction. A full queue or conversion failure rolls back the account claim, acceptance fields, receipt and document together. The existing bounded worker prepares the invoice PDF/email and uses the same lease, retry and uncertain-send controls as other public delivery. No renderer/transport executes in the acceptance HTTP path. Invoice-kind preparation suppresses delivery to an inactive account. Core migration `0009_public_invoice_delivery` records the additional receipt type; only isolated test databases were migrated.

Verification: **17 focused acceptance tests pass**, covering real CSRF enforcement on both routes, read-only GET, unsupported methods, foreign ownership/replay, expiry/invalid state, capacity/conversion rollback, shared throttles, worker preparation, stale conversion instances and concurrent safe retries yielding one invoice/job. Billing and portal-cart regressions pass, including materialized domain orders. The final focused acceptance/abuse/billing run has **78 passing tests**. Core/billing migration drift checks pass. Expanded browser verification has **48 passing desktop/mobile states**, including valid/expired confirmation and temporary failure; keyboard action-to-review navigation, accessible names, contrast and overflow pass. The mobile confirmation screenshot was visually inspected. These are Windows/SQLite/local fixture checks; live transport and PostgreSQL multi-worker execution remain external test scopes.

SEC-06 is still in progress pending the final public-mail inventory and any controls needed for account verification/reset delivery. The application roadmap's 20 product improvements remain pending; quote acceptance hardening is not being counted as completing the broader quote-lifecycle feature.

Final acceptance checkpoint regression: **1,044 tests passed**; the single unchanged failure remains the excluded nginx default-port assertion (80 expected, existing 780 configured). Django system checks, core/billing migration drift checks and whitespace checks pass. No customer database, deployment, live broker or mail provider was modified.


## SEC-06 completion audit: application work verified

The remaining account-mail inventory is complete. The allauth adapter queues registration/verification, password reset and account notices through encrypted UUID-bound envelopes in the shared outbox. No synchronous transport fallback is available. Bodies/addresses/token links are absent from plaintext job data. Worker preparation enforces the 15-minute queued-message expiry, account activation/session state and recipient association. Identical outstanding copies deduplicate across minute boundaries under the admission gate; completed recent copies have a 60-second cooldown. A later permitted resend can create a new copy. Ambiguous legacy case-variant reset addresses are bounded and fail without message fan-out. Optional login-code and account reauthentication forms have explicit early policies; no social provider/passkey signup is configured.

`core.0010_public_account_mail` adds the account kind and `core.0011_submission_digest_index` indexes duplicate lookup. These migrations were applied only to isolated test databases. Account token encryption reuses the versioned external keyring; missing keys fail closed. Worker operation, shared Redis behavior and real provider transport remain external runtime prerequisites, rather than changes made under this application-only scope. See [account mail delivery](account-mail-delivery.md) for the contract, expiry/resend rules and registration failure recovery.

| Acceptance requirement | Authoritative application evidence |
|---|---|
| Reject oversized/excessive requests before expensive work | Early middleware preceding CSRF, typed bounded quote/contact input, HMAC fixed-window shared counters and failure-closed cache handling; 46 abuse tests and 19 domain-bound tests. |
| Escape staff HTML email input | Autoescaped contact email template and encoded-payload assertions in test_public_abuse.py. |
| Avoid duplicate jobs/documents | Unique receipt transactions and conditional quote claims; 26 delivery tests, 17 acceptance tests and 20 account-mail tests, including real SQLite concurrent writers with safe retries. |
| Bound queued email/PDF work | Shared admission gate, three preparation attempts, leases/uncertain sends, credential-free subprocess execution, input/output/deadline caps; eight real process tests and nine renderer/download tests. |
| Include registration/reset/domain endpoints | Current route-policy inventory and allauth source checks; explicit worker execution in account validation/outbound tests; configured custom/allauth alias and method checks. |
| Make failures/retries accessible | Eight public error-contract tests; 52 desktop/mobile browser states including recovery, quote state retention, confirmation and queued reset/verification status. Contrast, accessible names, keyboard behavior and document overflow checks pass; representative mobile screenshots inspected. |

The eight focused public-security suites above contain **153 passing tests**. The final whole-application regression has **1,064 passing tests**; the single unchanged failure expects nginx port 80 while the existing configuration uses 780. Subsequent reset-status wording is covered by the final browser run, which explicitly checks the queued-delivery text. Django checks, accounts/core migration drift and whitespace checks pass. The existing initialization warning remains. Tests use Windows/SQLite, a public test encryption key and local mail; they do not establish PostgreSQL/Redis multi-worker or native PDF visual/runtime behavior. The known unpatched pdfkit advisory remains the separate SEC-03 exception; resource limits do not close it.

This verifies SEC-06's application acceptance criteria, not a deployed remediation or the entire roadmap. External trusted-proxy verification, key provisioning/data conversion, historical media/backup review and native/live-runtime validation remain recorded under their relevant workstreams. All 20 product improvements remain in scope and pending; next is item 1, reusable admin components and accessibility coverage.


## Product item 1 completion audit: admin components and accessibility

The reusable template library now provides page headers, action bars, cards/statistics, tables, pagination, fields, empty states, status labels, loading feedback and validation summaries. Customer/invoice lists, staff-role cards/forms and the user editor use it. Existing owner/role conditions stay in place. Shared pagination correctly URL-encodes and preserves multi-value filters while replacing page parameters. Semantic text/status tokens and the existing theme fixes remain active.

The drawer now manages modal focus and inert background content, wraps Tab/Shift+Tab, restores focus/scroll on Escape, close button and backdrop dismissal, and recovers focus across desktop/mobile resizing. It stacks above global banners. Table scroll regions have captions, header scopes and keyboard access; positioning the caption inside its container fixes the mobile document-overflow defect found during browser review. Admin scroll padding follows the visible cookie banner height, and loading feedback moves into the unobscured area. Invalid forms focus a linked error summary; fields/groups retain prefixed help/error associations.

| Acceptance requirement | Verified evidence |
|---|---|
| Reusable components cover every named type | Production component templates, template tag and migrated customer/invoice/staff-role/user-editor templates; contracts in admin-components.md. |
| Representative contrast and accessible names | Seven component tests include actual semantic CSS contrast ratios and escaping/class constraints; rendered browser checks cover the existing and new pages. |
| Errors are linked to fields | Prefixed single/grouped field unit cases; hidden-field error handling; browser summary-to-email focus navigation and invalid staff-role/user-editor states. |
| Drawers manage keyboard focus | Open focus, forward/backward wrapping, Escape/close/backdrop return, background inert state, desktop nonmodal behavior and responsive focus recovery exercised in Chrome. |
| Narrow actions and tables remain usable | 390-pixel owner customer/invoice pages have no document overflow; table regions scroll with ArrowRight; representative screenshots inspected at desktop/mobile sizes. |
| Include populated, empty, loading and error states | Populated/empty customer and invoice lists; actual intercepted native POST with pending feedback and preserved action data; invalid user/staff-role forms. Loading feedback is checked above the visible cookie banner. |
| Preserve existing theme fixes | All prior browser fixture states remain in the full run, alongside the new component states; reduced-motion styling and semantic focus outlines remain. |

Final verification: **1,071 application tests passed**; one unchanged failure is the excluded nginx default-port assertion (80 expected; existing 780 configured). **64 desktop/mobile browser states passed**, plus drawer/keyboard/resizing and pending-action interaction checks. Seven focused component tests pass. Django system checks and whitespace checks pass; the existing database-access-during-initialization warning remains. Representative customer, loading, invalid-form and drawer screenshots were inspected, and the final pending status is visible above the banner. Fixture scripts use isolated SQLite/local mail and intercept pending WHM POSTs; no live provider, customer database or deployment was changed. The browser fixtures do not replay production CSP headers or substitute for an assistive-technology audit.

Item 1 meets the roadmap's representative-page acceptance criteria. Future bespoke workflows still need their own state checks and should adopt the shared components. Items 2-20 remain pending, with permission-aware global search next. The full goal remains active.
