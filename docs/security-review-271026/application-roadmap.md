# Application improvement and feature plan — 27 October 2026

## Scope

Registrant validation implementation is organised into shared workstreams with explicit TLD membership, as specified in [validation-workstreams.md](validation-workstreams.md). Product 3 must use this structure rather than independent per-TLD KYC implementations; registry exceptions remain explicit.

This implementation roadmap is based on the repository and the security review in this folder. Its full scope is **20 product improvements and new features**, plus **nine application-security workstreams**. Current completion evidence and remaining work are tracked in [implementation-status.md](implementation-status.md).

Swarm, Docker, nginx, reverse-proxy configuration, network exposure, server processes, infrastructure changes, and deployment work are excluded. SR-003 remains outside this plan. Application portions of SR-004, SR-005, SR-006, and SR-008 are included with external prerequisites explicitly identified below; completing these portions does not close the infrastructure-dependent parts of those findings.

Existing capabilities include bulk renewal cart entry, saved payment methods, notification preferences, customer login history, hashed API keys, domain operations, DNS CRUD, quote conversion, hosting self-service, and administrative audit/email/webhook logs. The proposals below extend those capabilities or introduce additional workflows.

Priorities: **P0** = fix before adding sensitive capabilities; **P1** = next delivery priorities; **P2** = follow-on improvements. Sizes are relative engineering scope: **S** = contained change; **M** = several connected components; **L** = new workflow or shared data model. They are not calendar estimates.

## Application security workstreams

| ID | Priority / size | Proposed change | Completion criteria |
|---|---|---|---|
| SEC-01 / SR-001 | P0 / M | Close the MFA backend bypass. Use one consistent password/MFA policy across custom login, Django admin, and allauth session-creation paths. Throttle MFA attempts, expire pending challenges, reject inactive users at completion, and prevent TOTP replay. | Correct password alone never establishes a session for an MFA-enabled account. Tests use the normal backend list and cover expiry, retries, deactivation, backup-code reuse, and alternate login paths. |
| SEC-02 / SR-002 | P0 / L | Introduce explicit staff permissions for support, content, billing, domains, integrations, and account security. Reserve privilege changes, owner-account recovery, impersonation, secret export, and allowlist management for owners. Require recent MFA for sensitive actions. | A permission matrix covers every admin route. Ordinary staff cannot promote themselves, reset owner MFA, export secrets, or impersonate privileged users. Denied requests cause no side effects. |
| SEC-03 / SR-004 | P1 / M | Upgrade Python application dependencies to supported, patched versions after checking current upstream advisories and compatibility. Run a package advisory scan against the resulting dependency set. | Relevant application tests pass with the new pins; high/critical advisory results have a fix or a documented exception. Container, OS, and deployment upgrades are excluded. |
| SEC-04 / SR-005 | P1 / M | Centralise application IP parsing. Trust forwarding headers only when the socket peer is explicitly trusted; validate parsed addresses and use the same result for throttling, audit records, and allowlist checks. | Direct untrusted callers cannot spoof their address. Tests cover trusted and untrusted peers, malformed headers, IPv4/IPv6, and the documented proxy chain. Actual proxy configuration and boundary verification remain external prerequisites. |
| SEC-05 / SR-006 | P1 / L | Encrypt database-held integration credentials with authenticated encryption and key versioning. Stop HTTP handlers rewriting `.env`. Apply owner-only access, redaction, and recent-MFA checks to secret changes/import/export. | A database dump contains no usable plaintext integration secrets; migration is resumable and safely verified; failure to decrypt never silently selects an unintended credential. The encryption key must be provided outside the database. Key provisioning, credential rotation at providers, and historical backup cleanup remain separately coordinated prerequisites. |
| SEC-06 / SR-007 | P1 / M | Add bounded public-form payloads, typed validation, shared-cache throttles, email escaping, and bounded queued email/PDF work. Include registration, reset requests, and expensive domain searches in the abuse-control inventory. | Oversized or excessive requests are rejected before expensive work. HTML input is escaped in staff email. Duplicate submissions do not create duplicate jobs/documents. |
| SEC-07 / SR-008 | P1 / M | Replace unsafe catalogue JSON embedding with `json_script`; escape generated SVG text; decode, validate, and re-encode logo uploads to an allowed raster format. Review CSP handling so response-specific restrictions are preserved. | Payload tests cover script termination, SVG/XML injection, misleading extensions, oversized images, and active-content uploads. Separate media hosting is excluded, so the infrastructure-dependent media-origin risk is not marked closed. |
| SEC-08 / SR-009 | P1 / S–M | Bound backup uploads and archive expansion; require the expected structure, known setting keys, typed values, and an atomic import. | Oversized, high-ratio, duplicate-entry/key, unknown-key, and invalid-value archives are rejected without partial updates. Import uses SEC-02 and SEC-05 access/encryption controls. |
| SEC-09 / SR-010 | P1 / S | Return generic health states; keep diagnostic exceptions in internal logs with a correlation ID. | Anonymous responses never expose backend errors, credentials, hosts, ports, or stack traces. Existing health consumers retain a stable status contract. |

Security verification should include alternate authentication paths, object ownership, CSRF, HTTP method restrictions, payment-event replay, and authorization failures. Revalidate the original findings against the current code before closing them; a historical report is evidence to investigate, not a substitute for regression tests.

## The 20 product improvements and new features

### 1. Admin component library and accessibility coverage

**Priority:** P1 · **Size:** M · **Type:** Improvement  
**Build on:** `static/css/admin.css`, `templates/base_admin.html`, admin templates.

Continue the theme work with reusable template components for page headers, action bars, cards, tables, pagination, forms, empty states, and validation summaries. Define semantic text/status colours and keyboard interactions instead of copying styling between pages.

**Accept when:** Representative pages pass automated contrast/name checks and manual keyboard review; errors are linked to fields; drawers manage focus; narrow screens retain usable actions and table scrolling. Include populated, empty, loading, and error states. Existing theme fixes remain intact.

### 2. Permission-aware global search

**Priority:** P1 · **Size:** M · **Type:** New feature  
**Build on:** Admin user, domain, service, invoice, quote, and ticket lists.

Add one search entry point for email, customer name, domain, document number, and ticket reference. Return grouped results and direct links, with indexed lookups and bounded results.

**Accept when:** Search returns only records the actor may access; totals, autocomplete, and snippets do not reveal restricted records. Tests cover exact and partial matching, pagination, and permission changes.

### 3. Customer 360 workspace

**Priority:** P1 · **Size:** L · **Type:** New feature  
**Build on:** Accounts, business profiles, domains, services, billing, support, and audit data.

Create a unified staff customer page showing account health, owned assets, balances, open tickets, and a chronological activity timeline. Replace repeated navigation between unrelated lists with scoped actions.

Support both individual and business customers. Track portal email verification, local contact-data checks, business checks where applicable, registrar email verification, and TLD eligibility independently. Individuals must not need a company number or Companies House result. The detailed requirements and regression criteria in [individual and registrar validation](individual-and-registrar-validation.md) are part of this feature and the domain-order workflows below.

**Accept when:** Every panel respects permissions; restricted financial/security information is omitted for unauthorized roles; timeline entries link to source records; internal notes never appear in customer views.

### 4. Saved list views and configurable tables

**Priority:** P1 · **Size:** M · **Type:** Improvement  
**Build on:** Existing admin filters and pagination.

Persist named filters, column visibility, sorting, and page size per staff member. Start with domains, customers, invoices, and tickets, with sensible built-in views such as overdue invoices and expiring domains.

**Accept when:** Saved views survive sign-out, can be reset, validate field/filter choices, and cannot bypass server-side permissions. Export follows the visible filter and safe row limits.

### 5. Bulk domain operations with per-item outcomes

**Priority:** P1 · **Size:** L · **Type:** Improvement  
**Build on:** Existing bulk renewal cart entry and individual lock/nameserver operations.

Extend bulk actions to supported lock, auto-renew, nameserver, and contact changes. Show a preview, affected domains, costs where applicable, and a job result for each item.

**Accept when:** Each item is authorized independently; invalid or unsupported domains are explained; repeat submission does not repeat completed provider changes; partial failures can be retried selectively. Transfers/EPP disclosure remain separately gated sensitive actions.

**Additional required scope (30 October 2026): DNS provider selection and switching.** Domain signup, the customer portal and the admin portal must expose an explicit choice of Cloudflare DNS or WHM/platform DNS, with the current/custom delegation also visible. Selecting a provider must initiate the shared configuration workflow rather than merely update `dns_provider`. Provider selection is mutually exclusive; switching away from Cloudflare must offer the intended replacement delegation rather than leave DNS unconfigured.

**DNS switching acceptance:** Retain the signup selection through checkout/registration and the local handoff. For an existing domain, preview the target provider, complete nameserver list and affected website/mail records before confirmation. Provision or adopt the correctly scoped Cloudflare/WHM zone, apply the reviewed record plan, update ResellerClub delegation, and independently verify provider configuration and registrar state before declaring success. Show queued/running/review/partial-failure outcomes in both portals. Preserve existing mail/site records and DNSSEC requirements in the change plan; disabling a provider must not silently delete its zone or records. Customer ownership, admin permissions, registry workstreams, operation reservations, exact request replay and selective stage retries apply to this workflow. A timeout after a remote mutation must retain its submission marker for reconciliation. Signup and later portal switches must reuse the same services and stage evidence; neither a dropdown save nor a provider acknowledgement completes the workflow. Verify signup, customer and admin flows, both switch directions, preserved records, scope/ownership rejection, timeout/replay and mobile/desktop states before accepting this scope.

### 6. Renewal and payment-readiness centre

**Priority:** P1 · **Size:** M · **Type:** Improvement  
**Build on:** Domain expiry, auto-renew, saved cards, reminders, and billing records.

Provide 30/60/90-day renewal views with expected costs, auto-renew state, payment readiness, failed attempts, and actionable exceptions. Offer a consolidated renewal cart without implying payment is guaranteed.

**Accept when:** Dates and prices have a recorded source/freshness; reminders are deduplicated; failed payment and registrar failures are distinguishable; displayed eligibility reflects provider/TLD restrictions.

### 7. Domain transfer progress and recovery workflow

**Priority:** P2 · **Size:** L · **Type:** Improvement  
**Build on:** Transfer cart entry and domain-order tracking.

Show transfer milestones, outstanding customer actions, registrar messages, last checked time, and a supported retry/cancel path. Surface authorization-code errors and transfer-lock problems clearly.

**Accept when:** Provider status mappings are explicit; unknown statuses require review; auth codes are redacted and never logged; retry/cancel availability is checked server-side.

### 8. DNS change preview, history, and controlled rollback

**Priority:** P1 · **Size:** L · **Type:** Improvement  
**Build on:** DNS CRUD and provider synchronization.

Validate and preview DNS changes before submission, retain record-level history, and offer an inverse change to restore a previous state where still valid. Highlight mail and website impacts.

**Accept when:** Record types/values/TTL are validated; concurrent changes are detected; history records actor and provider outcome; rollback is an explicitly confirmed new operation, with provider failures visible.

### 9. Safe DNS presets for common services

**Priority:** P2 · **Size:** M · **Type:** New feature  
**Build on:** DNS forms and the change-preview workflow in item 8.

Add presets for basic hosting, email-provider verification, MX, SPF, and DMARC. Require tenant-specific values rather than inventing verification tokens or provider records.

**Accept when:** Users see the proposed diff; existing records are not silently replaced; duplicate SPF/MX conflicts are flagged; recommendations explain the information needed to complete them.

### 10. Customer-facing order and provisioning timeline

**Priority:** P1 · **Size:** M · **Type:** Improvement  
**Build on:** Domain orders, services, invoices, and provisioning jobs.

Explain progress from order to payment, provider processing, and activation. Show blockers and next actions while keeping internal stack traces and credentials out of the portal.

**Accept when:** Payment confirmation is distinguished from service activation; stale progress is visible; timelines survive retries; customers see only their own orders.

### 11. Integration exception and reconciliation workbench

**Priority:** P1 · **Size:** L · **Type:** Improvement  
**Build on:** Existing WHM inventory reconciliation, registrar diagnostics, and task results.

Unify mismatched ownership, missing provider identifiers, stale inventory, failed synchronization, and uncertain provider outcomes into an assigned exception queue. Show a proposed correction before applying it.

**Accept when:** Synchronization does not automatically transfer ownership or delete assets; resolving an exception records evidence and actor; conflicting updates require review; refreshes cannot duplicate services or orders.

### 12. Job progress and safe retry controls

**Priority:** P1 · **Size:** M–L · **Type:** Improvement  
**Build on:** Celery tasks, task results, provisioning jobs, and webhook logs.

Add a common application-level operation record with progress, correlation ID, attempt count, redacted failure reason, and retry eligibility. Distinguish terminal failures from operations whose provider outcome is unknown.

**Accept when:** Retry checks provider state or an idempotency key before repeating a charge, registration, renewal, or provisioning action; permissions are checked; cancellation is offered only where supported. No worker/deployment changes are included.

### 13. Quote lifecycle and acceptance improvements

**Priority:** P2 · **Size:** M · **Type:** Improvement  
**Build on:** Existing quote builder, public token links, acceptance, and invoice conversion.

Add versioned revisions, expiry reminders, acceptance summaries, and explicit confirmation after login. Make delivery/view/accept/convert status clear and permit revocation of shared links.

**Accept when:** Acceptance requires POST and a confirmation step; accepted content is retained; only one invoice is produced under concurrent/repeated acceptance; changed or expired quotes cannot be accepted accidentally.

### 14. Billing corrections, credit notes, and reconciliation

**Priority:** P1 · **Size:** L · **Type:** New workflow  
**Build on:** Invoices, payments, manual billing administration, and provider event records.

Add explicit credit notes, payment allocations, refund requests/outcomes, and an unmatched-payment queue. Preserve the original financial record instead of editing history to represent a correction.

**Accept when:** Currency and amount constraints hold; partial credits/refunds are tracked; operations require billing permissions; concurrent/replayed events cannot double-credit or double-refund. Tax/accounting requirements require business validation before implementation.

### 15. Support agent workspace

**Priority:** P1 · **Size:** L · **Type:** Improvement  
**Build on:** Tickets, departments, assignments, priorities, and internal-message fields.

Provide conversation-oriented staff ticket handling with assignment, internal notes, response templates, linked assets, response targets, and queue views for overdue/unassigned tickets.

**Accept when:** Internal notes and attachments stay private; customer replies update the right queue; assignment conflicts are handled; timing rules account for configured business hours. Support access follows SEC-02.

### 16. Searchable customer knowledge base

**Priority:** P2 · **Size:** M · **Type:** New feature  
**Build on:** Existing content editing and support entry points.

Add structured articles for DNS, renewals, transfers, invoices, and hosting troubleshooting, with drafts, revisions, related articles, and suggestions when opening a ticket.

**Accept when:** Only published articles are public; search excludes drafts; content uses safe rendering; users can still create a ticket; article feedback identifies outdated guidance.

### 17. Delegated customer-team access

**Priority:** P2 · **Size:** L · **Type:** New feature  
**Build on:** Business profiles and individual customer accounts.

Introduce organization membership and invitations with owner, billing, technical, and read-only roles. Make asset ownership and membership explicit instead of sharing account passwords.

**Accept when:** Invitations expire and are single-use; membership revocation removes access immediately; cross-organization access tests cover every affected view/job; at least one owner remains. Complete a separate ownership migration design before implementation.

### 18. Customer account security centre

**Priority:** P1 · **Size:** M–L · **Type:** Improvement  
**Build on:** MFA, backup codes, login history, and API-key management.

Show active sessions, last activity, recent security changes, backup-code status, and expiring API keys. Allow revoking a session or all other sessions, with recent authentication for sensitive changes.

**Accept when:** Revoked sessions fail on their next request; another user's sessions are never exposed; alerts are deduplicated; displayed device/location details are labelled approximate and minimize stored personal data.

### 19. Notification inbox and delivery visibility

**Priority:** P2 · **Size:** M · **Type:** Improvement  
**Build on:** Notification templates/preferences and outgoing email logs.

Add an in-app notification inbox with read state, links to affected records, delivery history, and retry visibility. Explain which security or contractual notifications are mandatory.

**Accept when:** Events do not produce duplicate messages; links respect ownership; mandatory notices cannot be disabled through marketing preferences; notification jobs recheck recipient membership and permissions.

### 20. Scoped API and signed event subscriptions

**Priority:** P2 · **Size:** L · **Type:** New feature  
**Build on:** Hashed API-key generation/revocation and existing domain/service models.

Start with a documented read-only API for owned domains, invoices, and service status. Add expiring scopes and key rotation. Introduce signed outbound events only after endpoint-validation and retry controls are designed.

**Accept when:** Revoked/expired keys and inactive users cannot authenticate; scope/ownership checks apply to each resource; requests are throttled and audited; signatures include replay protection. Outbound callbacks validate destinations, prohibit private/reserved targets, revalidate redirects/DNS, and use bounded delivery retries.

## Suggested implementation sequence

| Stage | Work | Dependency / exit condition |
|---|---|---|
| 0 — Authentication and authorization | SEC-01 and SEC-02 | MFA regression tests and staff permission-denial tests pass before exposing new sensitive actions. |
| 1 — Application hardening | SEC-03 through SEC-09 | Existing application behaviour passes regression tests. Infrastructure prerequisites are recorded as outstanding, not silently treated as solved. |
| 2 — Usability and visibility | Items 1, 4, 10, 12, and 18 | Reusable components and a consistent operation model support later screens. |
| 3 — Daily operations | Items 2, 3, 6, 11, and 15 | Search and customer context use the permission model; reconciliation uses safe jobs and explicit review. |
| 4 — Controlled asset and finance changes | Items 5, 8, 9, 13, and 14 | Retry/idempotency, audit history, and confirmation patterns are already available; DNS presets follow DNS preview/history. |
| 5 — Extended self-service | Items 7, 16, 17, 19, and 20 | Organization ownership is designed before team access; the API follows the final ownership model. |

Stages describe dependency order, not promised release dates. Split large items into independently reviewable vertical slices; avoid a single release containing all changes.

## Shared implementation rules

- Put authorization and state transitions in shared application services so admin views, customer views, jobs, and API routes use the same rules.
- Use database transactions and idempotency keys for local financial/order transitions. Do not hold a transaction open across slow provider calls; persist an operation and reconcile its result.
- Keep existing ownership checks during refactoring and expand tests for unauthorized, inactive, revoked, and concurrent requests.
- Exclude passwords, tokens, auth codes, raw provider payloads containing secrets, and private customer data from routine logs and UI error messages.
- Use backward-compatible migrations where practical. Validate migration behaviour on representative fixtures and test rollback/restore requirements before changing ownership or encrypted storage.
- Test visual states and keyboard navigation for new screens. Use automated contrast checks as a supplement to manual review, not a blanket accessibility claim.
- Prefer small releases with clear verification: what changed, who may use it, provider effects, failure behaviour, and how duplicate requests are handled.

## Recommended first implementation batch

Start with **SEC-01 (MFA), SEC-02 (staff permissions), SEC-07 (unsafe content/uploads), SEC-08 (bounded imports), and SEC-09 (health disclosure)**. These address the confirmed authentication issue and several contained application risks. Follow with dependency upgrades, trusted-IP handling, abuse controls, and encrypted secret storage with an explicit external-key prerequisite.

For the first product batch, prioritize **1 (shared admin components), 4 (saved views), 10 (order timeline), and 12 (job progress)**. They improve daily usability and establish reusable foundations for bulk actions, reconciliation, and customer support.
