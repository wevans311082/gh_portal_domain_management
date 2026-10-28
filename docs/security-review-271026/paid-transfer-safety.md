# Paid transfer admission and recovery

This increment addresses roadmap item 3's paid-transfer admission requirement. Item 3 remains in progress: remaining registry eligibility adapters and contact replacement still require implementation. Initial organization consent capture/default handling and withdrawal support tracking are now described in [organization-publication-consent.md](organization-publication-consent.md). Roadmap item 7's complete registry progress/recovery workflow is also separate from protecting the initial paid submission.

## Durable boundary

`DomainTransferAttempt` retains one preparation reservation and UUID worker fence per transfer. Before contact creation, the service locks and validates the customer, invoice owner/payment, canonical domain/TLD, every required contact role, contact accuracy, authorization-code shape and distinct nameservers. Individuals use accurate personal contact data without a company-number requirement. Unsupported specialized transfer policies remain gated.

The subject freezes transfer identity, financial values, invoice payment summary, role IDs, contact-data digests, a digest of the authorization code, nameservers and provider scope/customer. It never copies raw authorization codes or contact payloads into attempt history. Existing transfer/cart auth-code storage is not encrypted by this increment; secret retention/encryption for that workflow remains future transfer recovery work.

After immutable contact synchronization, the service rereads the subject and ready bindings under locks. A durable `submitting` marker and actual role IDs commit before the paid HTTP request. A second transaction holds the subject rows stable through the bounded request and atomic acknowledgement/audit. A timeout, invalid/boolean ID, explicit error, process loss or failed acknowledgement commit leaves `submitting`/`unknown`, which cannot start another transfer POST. Only preparation stopped before that marker becomes retryable `aborted`. A worker with a superseded token cannot submit or reset a newer attempt.

Acknowledged requests reuse the saved order, customer and nameservers. Changing the default customer cannot reinterpret an accepted order; changing the provider scope or transfer identity blocks reuse. Registration and transfer records for the same domain require review before either new paid workflow proceeds. Existing failed transfer records cannot be reinvoiced as fresh requests; checkout rechecks this before invoice creation.

The [ResellerClub transfer contract](https://manage.resellerclub.com/kb/answer/758) requires an invoice option and distinguishes an order response from registry progress. The API client sends `NoInvoice`, matching portal billing, and refuses HTTP redirects. Its HTTP adapter excludes POST from read/status retries. No application retry is treated as evidence that the original provider request failed.

## Local handoff and operations

The acknowledged order commits before local domain creation. Local handoff uses locked `get_or_create` and refuses another owner's domain or a different registrar order; it cannot overwrite ownership. Local failures retain the paid ID for safe resumption. A new domain is `pending` and the transfer stays `processing`, with no invented registration/expiry date, completion timestamp or DNS provisioning. An acknowledgement does not complete a registry transfer.

Domains linked to pending/uncertain transfers are held from registrar/DNS operations, renewal and EPP disclosure using the shared association guard. Consistent completed legacy assets retain their existing policy; captured scope mismatches still block provider reuse. Read-only staff transfer list/detail routes require `view_domains`, exclude billing/contact/auth-code/diagnostic payloads and recheck permissions each request. Customer notices show only owned, bounded requests and explain support review separately from registry completion.

## Historical migration

Migration `0018` creates unknown historical-review attempts for old transfers without retained evidence, including unpaid, failed and cancelled records. Only a consistently completed numeric-ID domain association is exempt. It uses historical models, 500-row batches and no provider calls, and copies neither authorization codes nor old error bodies. A real isolated `0017 -> 0018` migration check quarantines four representative uncertain records and preserves a consistent completed asset. A separate regression covers more than 500 records and idempotent rescanning.

Do not let old worker code continue processing historical requests during migration. No deployment or production migration was performed here. PostgreSQL locking/process-loss behaviour and funded provider contracts remain externally unverified.

## Audited owner recovery

Use an active owner, the staff page's attempt reference, a provider case/reference and a 10-1000 character investigation reason. These commands never purchase, charge, activate a domain or send mail. Do not paste secrets or provider response bodies into the reason.

```text
python manage.py reconcile_transfer --attempt-id ID --owner-id ID --action release-preparation --provider-reference CASE --reason "Stopped and reviewed the original preparation worker"
python manage.py reconcile_transfer --attempt-id ID --owner-id ID --action link-order --remote-order-id ID --provider-reference CASE --reason "Provider support confirmed the original transfer order"
python manage.py reconcile_transfer --attempt-id ID --owner-id ID --action adopt-legacy --remote-order-id ID --customer-id ID --provider-reference CASE --reason "Provider support confirmed the historical transfer association"
python manage.py reconcile_transfer --attempt-id ID --owner-id ID --action confirm-no-order --provider-reference CASE --reason "Provider support confirmed no existing or pending transfer order"
```

`release-preparation` only releases preparation older than 15 minutes without a paid marker; it rotates the fence. `link-order` strictly matches provider order/domain/customer and all retained role IDs in the original provider scope. Recent submitting attempts cannot be reconciled for at least 15 minutes. Historical adoption requires explicit provider customer identity and validated status/date/nameserver facts. It retains a pending local handoff rather than inferring registry completion. No-order confirmation is an explicit owner attestation, restricted to unlinked historical blank/invalid-ID requests; it cannot erase a known order or release modern uncertainty. Payment state is unchanged.

Owner authority, transfer identity, retained state/token and linkage are checked again under locks after provider reads. Recovery and its audit commit atomically. Requeueing the existing transfer task after acceptance resumes local handoff through the retained order; it cannot perform another transfer purchase. Full customer/staff progress polling and an HTTP recovery action remain item 7 work.

## Diagnostic secrecy and checks

Provider errors can echo secrets. Normal GET/POST transport now emits authored generic errors and logs only operation metadata. The optional debug tray captures method, endpoint and HTTP status only. Owner HTTP diagnostics also exclude prepared URLs, headers, parameters, response bodies and raw exceptions; submitted parameter JSON is cleared. Requests refuse redirects. This protects new transfer codes and closes the shared debug/error disclosure path found while tracing the worker.

The broad suite passed **1,576 tests**, with the existing excluded nginx port-80/780 assertion as its sole failure. Earlier combined authorization/transfer checks passed **388 tests**. Final focused checks and desktop/mobile verification are recorded in implementation-status.md. Tests cover duplicate workers, stale fences, contact/price/auth-code changes, cross-workflow conflicts, payment ownership, malformed acknowledgements, timeout and audit rollback, local failure recovery, foreign ownership, historical migration/reconciliation, role loss and diagnostic secrecy. No production/provider operation was used as verification.
