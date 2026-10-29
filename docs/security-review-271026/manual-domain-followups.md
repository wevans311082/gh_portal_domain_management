# Retained manual domain follow-ups

Implemented 29 October 2026 to preserve the manual cash/offline registration workflow across customer agreement review. Saving a request retains the selected hosting package, annual price, optional invoice/subtotal override and notes. It does not register the domain, provision hosting, create an invoice or manufacture customer acceptance.

## Capture and authority

`ManualDomainFollowup` is an additive, protected one-to-one record on the domain order. The exact plan and digest cannot be replaced through model saves. Protected package, service, invoice and job associations retain the history. The digest detects inconsistent records; it is not a signature against an actor who can rewrite the database and every related digest.

Capture checks the current staff account and domain permission, plus hosting and billing permissions for the requested choices. The selected customer must be an active ordinary account. Hosting requires an active WHM package and a known WHM host/account. The plan records the original prices and label, package routing/configuration digest and WHM endpoint/account scope without copying an API token. Invoice overrides must be finite, non-negative, within the invoice limit and have at most two decimal places. Notes are bounded to 4,000 characters. The manual POST has a 64 KiB body bound and shared request limits.

Order and plan creation share a transaction. Capture audits contain identifiers rather than notes or credentials. Existing or cancelled requests cannot create a colliding order. Unsubmitted requests with saved choices can be cancelled; deletion does not discard their retained history. No plans, services, jobs or invoices are inferred for historical orders.

## Agreement delay and local completion

The customer uses the same ordinary-customer agreement-review flow as other direct requests. Staff cannot accept on behalf of a customer. Processing the retained order rechecks every permission required by the saved choices before changing its status or calling the registrar.

After registration, local preparation requires the unchanged recorded request, an active customer, a matching owned domain/provider order and retained registrar acknowledgement. Package routing/configuration or WHM account changes require review. Price and label changes do not silently change the captured hosting price or invoice wording. Existing unrelated hosting or invoice associations require review instead of allocating another resource.

Preparation locks the customer, domain, order and follow-up record, then creates at most one service, deterministic provisioning job and requested invoice. All rows, associations, preparation marker and audit commit together. An invoice/audit failure rolls back the entire local preparation. The original manual billing rules remain: optional unpaid post-registration invoice, captured domain/hosting prices or the explicit subtotal override, existing invoice tax handling and no automatic invoice email.

The normal staff process action performs these follow-ups after registration. Reopening an already completed domain resumes only unfinished saved work; it does not purchase the domain again. Prepared associations are checked before reuse.

## Queue delivery and hosting fence

Queue delivery starts only after local preparation commits. Each delivery has a durable marker and UUID token. A late failed acknowledgement cannot overwrite a newer delivery result. If the broker outcome is uncertain and no worker has started, an authorised operator can redeliver the same retained job without allocating another service, job or invoice.

The provisioning worker recognises these manual services, including attempts to send a replacement generic job. Only the retained job may proceed. Before a provider mutation it rechecks current actor authority, customer/domain linkage, package/configuration and effective WHM account, and commits a separate worker-start fence. It uses the WHM client frozen at admission rather than constructing a request against a subsequently changed endpoint/account. Duplicate or overlapping delivery cannot start another hosting mutation.

A pre-start authority/configuration rejection leaves the job available for authorised correction and redelivery. Once the worker starts, an uncertain provider error does not automatically retry account creation. The service/job instead need provider review, with a generic retained error rather than a raw response. This fence applies to these new manual jobs; it is not a claim that every legacy provisioning workflow was redesigned.

## Staff screens

The order detail shows saved choices, retained records, delivery and job states separately. Billing permissions control notes, prices and invoice details; service permissions control package/job details. Domain-only staff do not receive those fields in the summary. Integrity failures withhold use of the plan. Resume appears only for unfinished safe work; a started worker needing provider review is identified explicitly. Text and notes are escaped, including literal script text. The manual form describes saving and customer review, and labels the amount as an invoice subtotal override.

## Migration and verification

`admin_tools/0005_manual_domain_followups.py` creates only the new record and constraints. Unrelated WizardProgress/status-choice drift was excluded. `scripts/verify_manual_followup_migration.py` upgrades an isolated in-memory database containing a historical order, service and job, proving that they remain unchanged and no follow-up is inferred. No production migration was applied.

Tests cover capture, immutable/checksummed choices, delayed registration, original price retention, changed routing/account scope, local/audit rollback, repeated preparation, broker failure and late acknowledgements, the retained-job worker fence, overlapping delivery, provider uncertainty, alternate-job bypass, permission revocation, role-filtered summaries, invalid amounts, cancellation/deletion history and the complete staff/customer/staff journey. Browser fixtures use isolated data and mocked registrar/broker calls. Live broker/provider behaviour and PostgreSQL concurrency remain operational checks; no live provisioning, registration or email was performed by this work.

Final verification: **2,294 passed, one unchanged deployment assertion excluded**. Thirty-three new focused cases cover this workflow, including the complete HTTP staff/customer/staff journey. Twelve desktop/mobile states cover waiting, ambiguous delivery, prepared records, integrity failure, a domain-only operator and the revised manual form. Keyboard focus, a 40px minimum resume target, cookie-banner clearance, contrast, names and inert script text are checked. Ready and role-filtered mobile screenshots were inspected; final card screenshots also retain the complete choices. Django and diff checks pass. Migration dry-run reports only the previously documented unrelated drift in WizardProgress, DomainOrder status choices, support and website_templates; none was included in migration 0005.

Additional credential handling: future provisioning success jobs retain only username, provider and acknowledgement metadata. Opaque provider success payloads are not copied to the database; a mocked response containing a password verifies this boundary. Historical stored outputs were not rewritten. Final validation also rejects control characters in notes and overlong/control-containing WHM package names before persistence. The form explicitly identifies invoice notes as customer-visible.
