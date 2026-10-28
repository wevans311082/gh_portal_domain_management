# Customer workspace implementation

The staff customer workspace combines account health, assets, financial balances, open support counts, contact checks, activity and internal notes. It is linked from the customer list and customer search records. Staff and owner accounts are excluded as customer targets. Viewing the workspace does not call a provider, send email or modify customer state.

## Permission boundaries

| Information or action | Required access |
|---|---|
| Workspace, account state, portal email state and general notes | Active staff with `view_customers` |
| Domains, registrar email evidence and contact checks | Additionally `view_domains` |
| Hosting services | Additionally `view_services` |
| Invoices, quotes, balances and billing notes | Additionally `view_billing` |
| Tickets and open ticket count | Additionally `view_support` |
| MFA enabled state and security notes | Additionally `view_audit` |
| Add general notes | `view_customers` and `manage_customer_notes` |
| Add billing notes | Additionally `view_billing` and `manage_billing` |
| Add security notes | Owner with recent MFA |
| Permanently redact note text | Owner with recent MFA |

Owners retain their existing privileged access. Migration 0004 adds the note permission to Portal Support; arbitrary existing staff receive no new role. Other role definitions remain unchanged. Fresh actor authorization is checked again under the write transaction. Notes do not grant access to the source customer's financial, security or domain actions. Source links and manage actions retain their own authorization checks. Responses prohibit caching.

Note visibility filters apply before pagination, counts, timeline construction and direct note lookup. Note authors receive no exemption after losing a relevant permission. A note cannot change audience or body through replay. General note guidance prohibits restricted financial/security material; classification depends on the author choosing the correct audience, rather than attempting to infer sensitive meaning from free text.

## Panels and activity

Panels show five recent records, with full customer-scoped pagination of 20 records for a selected panel. Balances subtract recorded payments from unpaid, overdue and partially paid invoices, clamp negative outstanding amounts to zero, and group currencies separately. Draft, paid and void invoices are excluded. The first 20 currency groups are displayed with an explicit truncation message if necessary. There is no combined multi-currency total.

Portal email verification is matched to the current customer email. General contact completeness is displayed separately from current business-check evidence, registrar email verification and TLD eligibility. An individual's business check is not applicable; no company number or business verification is required. Missing or stale evidence does not become verified. The workspace does not infer universal eligibility or identity verification from complete fields or provider email evidence. The contact preview is explicitly limited to five recent contacts.

The activity timeline merges permitted record creation and most recent update timestamps, with direct source links. It does not claim to reconstruct intermediate historical changes from current records. Raw audit payloads, credentials and note bodies are excluded. Up to 21 candidates per source/event type are fetched for a 20-entry page. Timestamp/source/ID ordering gives deterministic ties; signed customer-bound cursors expire after one hour. Every subsequent request recomputes permissions. The feed reflects live records, rather than an immutable database snapshot; the latest-activity link starts fresh after new records or permission changes.

Application migrations add customer/timestamp/ID indexes for domain, service, invoice, quote and ticket history, plus the internal-note history index. SQLite EXPLAIN verifies that actual workspace domain timeline queries use the created/updated indexes. Live PostgreSQL query plans and production index creation have not been performed.

## Note integrity and redaction

Notes are staff-only plain text, bounded to 4,000 characters, escaped on output and preserved across validation errors. The note POST accepts at most 64 KiB so maximum UTF-8 text still fits URL-encoded forms. Shared request limits fail closed. CSRF is required. A unique UUID submission key makes retries idempotent and rejects cross-customer, cross-author, text or visibility changes. The customer row serializes admission; note and metadata-only audit commit together. Browser saving state blocks repeated submission and preserves typed text.

There is no ordinary edit endpoint. Owner redaction clears the live note body, records actor/time, and retains the reference and metadata audit. Repeating redaction has no additional effect. Audit failure rolls back both creation and redaction. Audit data contains note/customer identifiers and visibility, not the note text. Redaction does not claim to erase database backups. No customer page or notification consumes the internal-note model; customer requests to staff note routes cannot read it.

## Verification and remaining scope

Focused tests cover panel and timeline filtering, role revocation, privileged-account exclusion, per-currency balances, individual checks, deterministic cursor pagination, cursor tampering/customer binding, source indexes, note audiences, direct-link filtering, CSRF, request bounds, shared-limit failure, Unicode text, escaping, replay, recent MFA, redaction and audit rollback. Browser fixtures cover desktop/mobile populated, empty, invalid, saving, Support, Billing and redacted-note states; checks include contrast, accessible names, linked error focus, long text, repeated submission and cookie-banner clearance. Representative validation and note screenshots were inspected.

This delivers the workspace UI and its permission-filtered timeline/notes. Roadmap item 3 remains in progress because its broader individual/registrar requirements include workflows and TLD adapters still listed in [individual-and-registrar-validation.md](individual-and-registrar-validation.md). A review gate for an unsupported extension is not counted as delivery of its adapter. No later roadmap feature is marked complete by this workspace milestone.
