# Paid registration submission safety

## Scope and current status

This increment protects registration requests handled through the new `DomainRegistrationAttempt` record. It is part of roadmap item 3's contact-version and duplicate-paid-submission requirements. It does not close the complete requirement: historical uncertain orders now have the separate [historical review workflow](historical-registration-review.md); initial paid transfers now have [equivalent admission safeguards](paid-transfer-safety.md); broader DNS/provisioning retry guarantees remain outstanding. Item 3 and the full roadmap remain in progress.

The provider [Register contract](https://manage.resellerclub.com/kb/answer/752) returns a registration order ID and action details. A retained order acknowledgement is separate from identity, registrar email verification, registry eligibility and subsequent local completion. Provider registration responses and diagnostic bodies are not copied into attempt history.

## Admission and version checks

One retained attempt exists per order. Its durable preparation reservation commits before contact creation. Concurrent preparation is refused. A UUID token fences a delayed worker after an operator releases stale preparation. Attempts and their orders cannot be deleted through the ordinary staff action, including an attempt stopped before purchase; history is retained.

The subject includes customer/invoice identity, canonical domain/TLD, term, prices, privacy, auto-renew, DNS provider, role contact IDs, hashed contact-data versions and the nameservers selected for the attempt. It does not contain raw contact names, addresses, email, phone or credentials. The invoice summary freezes currency, total and recorded payment amount during preparation. The provider environment/reseller fingerprint and customer ID are retained separately.

Before the first contact mutation, the worker checks the current customer, invoice ownership/payment, canonical domain, policy, every required contact role and contact accuracy. Existing local domains cannot be purchased again through a fresh attempt. The application bounds nameserver counts to 2-13, and to 2-10 for the reviewed UK policy. UK unsupported privacy is corrected before the snapshot is taken.

After immutable contacts are synchronized, all relevant rows are reread under locks. An edit to an earlier role while a later contact is being synchronized invalidates the whole preparation. Each role must still have a ready binding for its current payload, customer, contact type and provider scope. Reusing a remote ID from a different version is refused.

The `submitting` marker and actual role IDs commit before the paid HTTP boundary. A second transaction locks the order, customer, invoice and contacts, rechecks the snapshot and ready bindings, and holds those rows stable through the bounded registrar request. A process loss, timeout, malformed response, failed acknowledgement audit or uncertain local commit leaves a retained `submitting`/`unknown` state. It cannot automatically start another registration POST. Local validation failures before that marker become `aborted`, allowing a corrected preparation to be retried. A separate uncertain contact creation still has its own reconciliation requirement.

Acknowledged numeric IDs and the metadata-only audit commit together before domain/DNS completion. Subsequent processing reuses that ID and frozen nameservers; changed order identity, role IDs, provider scope or remote linkage requires review. A previously stored legacy registrar ID enters historical owner review before local completion; it is never accepted solely because an ID was pasted or saved. See historical-registration-review.md for read-only adoption and migration rules.

## Local completion and staff controls

The local domain is created under row locks and cannot overwrite a record belonging to another customer or another registrar order. An existing matching domain's state is preserved. Domain linkage commits before the existing DNS workflow, so a later DNS error does not discard the paid registrar ID or previously saved local linkage. A repeat can resume local completion without another registration purchase. This is not a claim that every external DNS mutation is exactly-once; DNS/provisioning reconciliation is still separate roadmap work.

The order page distinguishes preparation, submission started, review required, acknowledgement and a pre-submission stop. It displays state-update time and retained nameservers. Acknowledged orders offer **Resume local completion**. In-progress/uncertain orders do not offer another Process action. Normal edit, cancel, delete, mark-paid and retry routes recheck retained state under the order lock; hidden or disabled controls are not the authorization boundary. Clearing a displayed error cannot reset an attempt. Relevant form labels and responsive layout were corrected while adding these states.

Local authored validation guidance remains available to staff. Raw provider exceptions are excluded from the order error and registration log message; uncertain paid errors are not Celery-retried.

## Operator reconciliation

The command requires an active owner account for audit attribution, a provider/review reference and a bounded reason. It never registers a domain, issues a payment, activates a domain, marks verification passed or sends email. Do not copy credentials or provider bodies into the reason.

- **Release stale preparation:** review/stop the original worker, then run `manage.py reconcile_registration --attempt-id ID --owner-id ID --action release-preparation --provider-reference CASE --reason "Reviewed and stopped the original preparation worker"`. Only preparation older than 15 minutes with no paid marker can be released. The token rotates; a delayed old worker cannot reach the paid call. This cannot release a `submitting` or `unknown` attempt.
- **Link an uncertain registrar order:** after provider-side investigation, run `manage.py reconcile_registration --attempt-id ID --owner-id ID --action link-order --remote-order-id ID --provider-reference CASE --reason "Provider support confirmed the original order"`. The command reads the provider in the retained environment and requires an exact order, domain, customer and all required role-ID match. A recent `submitting` attempt is refused for at least 15 minutes. State/token are rechecked under locks after the read. A changed local identity or conflicting existing ID cannot be overwritten. Only the safe matched linkage and metadata audit are saved.

If the provider cannot establish what happened, leave the paid attempt uncertain. There is deliberately no blind paid retry switch. A stopped preparation may be released without inventing proof about a paid request because no paid marker was reached.

## Verification and remaining work

Application tests use isolated SQLite and mocked provider clients. They cover overlap, earlier-contact edits, order/price changes, changes between marker and HTTP, stale worker fencing, malformed/boolean acknowledgements, timeout, audit rollback after remote success, replay, known legacy-ID quarantine, role-link tampering, worker retry/error redaction, DNS failure recovery, foreign-domain conflicts, staff action protection and strict read-only reconciliation.

The final focused domain/order run passed 96 tests; an earlier combined authorization run passed 417 tests before the last replay/link refinements. The whole suite passed 1,447 tests with only the existing excluded nginx port-80/780 assertion. The final local-error guidance refinement passed 56 relevant tests. Ten desktop/mobile browser states cover all five attempt states, contrast, labels, responsive overflow, protected controls and local-resume wording. Representative mobile review and desktop acknowledgement screenshots were inspected. Django and whitespace checks pass; the domains migration dry run reports only the pre-existing status-choice drift. Migration 0016 adds the new attempt model without incorporating that unrelated drift.

Live PostgreSQL lock/process-loss behaviour and funded provider sandbox contracts have not been exercised. No production migration or registrar purchase was performed. Historical uncertain-order handling is now implemented in the linked review increment. Initial paid-transfer admission is now implemented in paid-transfer-safety.md. Next required work is remaining registry adapters, organization consent and contact replacement requirements. The existing handling of registrar activation/provisioning status is not certified by an order-ID acknowledgement.
