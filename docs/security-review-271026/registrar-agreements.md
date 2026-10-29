# Shared registrar agreement foundation

Implemented 29 October 2026 as part of the remaining registrant-validation requirements. The foundation, owner publication workflow and customer cart/direct-request acceptance are implemented, **not completed checkout enforcement**. Invoice/quote gates and paid-worker integration still need implementation. Existing order admission has not silently been described as protected by these new services.

## Published versions and shared assignments

`RegistrarAgreementVersion` retains exact text, title, version, optional HTTPS source reference, selected shared profiles/operations, publisher, publication time and a content/metadata digest. Versions cannot be edited through model saves or deleted through the record API. Document history is protected by foreign keys; unexpected content changes are detected when building a plan. This detects inconsistencies; a digest is not a signature proving resistance to an attacker who can rewrite the database and all related hashes.

`RegistrarAgreementBinding` selects the current document for an agreement key, shared profile and operation. Publication switches the selected bindings atomically and retains earlier versions. Publishing for a subset of profiles leaves other assignments unchanged. An identical UUID-keyed publication retry returns its earlier result without reactivating an obsolete version. Reusing its key for changed text/scope, or overwriting an existing agreement/version pair, is rejected.

The initial shared requirement key is `domain-service`; a profile can require additional keys. Profiles come from the reviewed catalogue: `general_raa`, `general_registry`, `uk_accuracy` and `us_nexus`. A single text version can be assigned across these lists. Publishing a document never enables an unsupported TLD, a prepared EU profile or a registrar-specific transfer workflow. No default legal text, documents, assignments or customer acceptance are created by migration 0026.

## Publication command

An operator can publish an approved local UTF-8 JSON file:

```powershell
.venv/Scripts/python.exe manage.py publish_registrar_agreement --owner-id <active-owner-id> --document-file <approved-json-file> --approved
```

Shell access is the operator authentication boundary. The active owner ID supplies checked publication authority and audit attribution; it is not a substitute for an authenticated HTTP session or recent MFA. The owner web workflow applies both before calling the same service.

## Owner publication screens

Registrar agreements is an owner-only Services navigation entry. Both the publication/history page and retained-version detail require recent MFA and prohibit caching. There is no HTTP edit/delete endpoint for a published version. Staff without owner authority cannot read the pages or submit publication requests.

Preview performs no publication. It displays exact escaped text, source reference, shared groups with their TLD lists and selected operations. A signed, owner-bound, 30-minute preview token binds that data and the group membership. Every new preview clears the approval checkbox. Publishing requires explicit approval and a matching preview; edited text, version or scope must be previewed again. Duplicate scalar fields, duplicate selections and unknown form fields are rejected. POST bodies are bounded to 256 KiB with shared request limits and CSRF protection.

The token also captures the publication catalogue revision. Publication checks that revision under the same transaction lock as its binding changes, so a stale preview cannot silently replace an intervening owner's publication. Exact retries retain their original document without reactivating it or repeating the audit. The detail page shows current assignments separately from retained history. Integrity failures retain escaped text for owner review while withholding the source link. These checks do not invent customer acceptance.

The owner pages explicitly state that customer cart/direct-request review is available but payment enforcement remains incomplete, so publication alone cannot be mistaken for complete checkout protection.

The JSON object requires `submission_key` (a fresh UUID), `agreement_key`, `version`, `title`, `body`, `profile_keys` and `operations`; `source_url` is optional. `body` must contain the exact approved text, not a generated placeholder. Supported operations are `registration` and `transfer`. The file is bounded to 256 KiB; title/text are bounded to 200/20,000 characters, and references must use HTTPS without embedded credentials. Duplicate/unknown fields and malformed scopes are rejected. Source URLs are references only: no remote document is fetched or executed. Command output contains identifiers and outcome, not the document body. A metadata-only audit and all binding changes commit together.

## Acceptance evidence

`current_agreement_plan` reads the complete required document set for the actual domain suffix, reviewed profile and operation. Missing documents require review, rather than an empty approval. The plan digest includes the domain, operation, profile and exact version manifest.

`record_agreement_acceptance` requires an active ordinary customer, an owned current contact, explicit acceptance, a matching contact digest and the plan actually presented. Owner/staff calls cannot substitute for the customer's act. Receipts and protected document links are immutable history. Replays cannot change customer, domain, operation, contact or document set, and a repeated old receipt never silently accepts newer terms. Changes to the contact or applicable terms require new acceptance. Missing/altered document links and future timestamps cannot count as valid evidence. Acceptance does not create identity, business, registrar-email or registry-eligibility verification.

The receipt service assumes its HTTP caller has authenticated the actual customer. The customer cart review route now rejects staff and impersonation sessions, applies CSRF, 32 KiB request bounds and shared limits, and rejects duplicate or unexpected fields. GET is read-only. Missing terms, unsupported operations, missing owned contacts and closed/submitted carts withhold the acceptance form.

The page shows exact escaped text, versions and registrant details with an unchecked acceptance control. A customer/item-bound signed review expires after 30 minutes and captures the contact, agreement plan and a secret-keyed digest of substantive cart values. Transfer secrets are never placed in the page or audit. Changed requests, contacts or terms require a fresh review; errors clear approval instead of silently carrying it to new terms. The attachment transaction rechecks current terms even on an old UUID replay, preventing an obsolete receipt from replacing newer acceptance. Audit failure rolls back both receipt and attachment.

Nullable protected receipt fields on cart items, registration orders and transfers preserve historical records without inferring acceptance (domains 0027; portal 0005). Cart materialization copies the explicitly attached receipt to the resulting request. This attachment is evidence storage, not yet an invoice or paid-admission gate.

## Direct-request customer review

`/domains/agreements/` lists only the authenticated customer's unadmitted registration and transfer requests, with separate bounded pagination. My domains links to this page. Closed, already associated, admitted or uncertain requests are excluded; the direct review route also checks the hold independently under request/attempt locks before writing. Staff and impersonation sessions cannot list, review or accept on behalf of a customer.

Direct registration/transfer pages share the cart form, exact escaped document rendering, signed 30-minute review and explicit unchecked acceptance. Tokens bind the customer, request kind and ID, registrant/contact version, plan and substantive request values; changing a transfer secret invalidates review without exposing the secret. Actual contact telephone and company number are displayed when present; US reviews also show the saved nexus category and intended purpose, separately from independent verification.

A paid request can still need customer acceptance if no submission has started. An unsubmitted aborted attempt can be reviewed again. Preparing, submitting, accepted, unknown, historical or previously submitted attempts cannot have their receipt replaced through this flow, even if an attempt is labelled aborted. Existing provider/domain associations and completed, cancelled or paused requests remain held. Foreign invoice associations and mismatched domain/extension data require support review. Acceptance never starts a provider request, creates an invoice or charges the customer. Old receipt retries are checked against current terms before attachment; an audit failure rolls back the whole receipt/attachment transaction.

The shared service and view do not claim that existing invoice or worker entry points already enforce this evidence. Required gates below remain separate work.

## Required integration work

Customer review and receipt attachment for cart and direct registration/transfer requests, including cart-to-order copying, are implemented. Staff requests must await real customer acceptance rather than manufacturing a receipt. Recheck authority, current contact and required versions before invoice/quote conversion and durable paid admission. Preserve already acknowledged provider actions and historical orders without inventing acceptance or resubmitting them.

`assert_acceptance_current` currently takes a publication catalogue lock while validating a receipt. Integration must use this only in short admission transactions, freeze the validated evidence in the durable submission marker, and validate that frozen evidence during the subsequent provider call. Do not hold the global publication lock across provider HTTP. Define publication cutover at durable admission; later terms must not retroactively rewrite an already admitted request's accepted text.

Customer screens use the same accessibility and authority checks across cart and direct-request workflows. Approved document content and the operator's legal review remain actual prerequisites; this code cannot certify that a document is legally sufficient for a registry or jurisdiction.

## Verification

45 focused service/command tests cover shared membership, missing/additional agreements, unsupported workflows, exact scope, explicit authority, stale plans/contact edits, replay, document changes, owner/customer revocation, integrity, file bounds and audit rollback. The isolated `scripts/verify_agreement_migration.py` upgrade checks 0025 to 0026 against historical order/contact data: no agreement, binding or receipt is inferred, and unrelated order-status choices remain unchanged. The final suite passed 2,104 tests with the unchanged deployment assertion excluded. Django checks pass. Migration dry-run reports only the previously documented DomainOrder status-choice drift; it was excluded from the agreement migration. No production migration, real agreement publication, provider request or email occurred. No browser validation is claimed for UI that is not yet implemented.

The subsequent owner-screen increment passed the full suite: **2,125 tests**, with the same deployment assertion excluded. Tests cover preview/approval, changed data/group membership, expiry, concurrent publication, exact replay, unauthorized access, recent MFA, CSRF, body limits, immutable detail and unsafe source suppression. Fourteen desktop/mobile states cover empty/error/preview/unapproved/published/retained/integrity screens. Four additional states verify publish-button clearance above the cookie banner and capture approval-field screenshots. Keyboard checkbox operation, linked error focus, contrast, accessible names, long text wrapping and inactive script text are checked; mobile preview and approval screenshots were inspected. Django and diff checks pass. No real agreement was published outside isolated test fixtures.

Customer cart increment: 22 added tests cover request/contact/terms changes, exact replay, stale-receipt replacement prevention, attachment audit rollback, ownership/staff/impersonation, closed carts, missing terms/contact, CSRF/duplicates and receipt copying to both registration and transfer requests. Full suite: **2,147 passed, one unchanged deployment assertion excluded**. Eight desktop/mobile states pass accessibility, keyboard checkbox and cookie-banner button-clearance checks; mobile review and acceptance screenshots were inspected. The isolated upgrade now covers domains 0025 to 0027 and portal 0004 to 0005, preserving historical cart/order data with null receipt fields. Django and diff checks pass. Dry-run reports unrelated existing migration drift in admin_tools, DomainOrder status choices, support and website_templates; these were not bundled into agreement migrations.

Direct-request increment: **53 added tests** cover both operation kinds, accepted/uncertain/historical/aborted submission states, closed requests, source mutations, changed contacts/terms, expired and wrong-kind tokens, ownership, staff/impersonation, revoked customer authority, atomic audit rollback, pagination, US declaration rendering, unsupported/prepared workstreams, CSRF, duplicate fields and request bounds. Final suite: **2,200 passed, one unchanged deployment assertion excluded**. Twenty-two desktop/mobile agreement states (14 direct-request/list states and 8 cart states) passed accessibility, keyboard and cookie-banner button-clearance checks. Mobile request-list and direct-review screenshots were inspected. Django and diff checks pass. No production provider request, real terms publication, migration or email occurred.
