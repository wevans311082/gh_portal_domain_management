# Historical registration review and provider holds

This increment extends roadmap item 3's paid registration safeguards to older records. It does not complete item 3, paid transfer safety, all registry adapters, contact replacement or the broader roadmap. Individuals remain valid registrants without a company number; identity, business checks, registrar email verification and TLD eligibility remain independent.

## Migration and retained state

Migration `domains.0017` creates a retained `unknown` registration attempt with `legacy_review_required=True` for historical orders without an attempt, including draft/unpaid orders: a local status cannot establish whether an earlier provider POST happened. A completed order is exempt only when it has a positive numeric registrar ID and a consistently linked domain with the same name, owner and registrar ID. Existing modern attempts are preserved.

Early accepted legacy-ID bootstrap attempts are also quarantined when the latest relevant audit proof is `domain.registration.legacy_link_retained`, with no paid submission or later reconciliation proof. Their worker token rotates. The migration uses historical models, bounded 500-row batches and no provider access. It retains order metadata, local role IDs and a digest of the old registrar ID, without copying contact payloads or old diagnostic bodies. Reversing schema changes does not restore permission to purchase uncertain orders.

The worker checks the hold before its completed-order shortcut. Newly encountered pasted registrar IDs also enter durable historical review instead of becoming accepted automatically. Ordinary staff actions cannot clear the hold, delete retained attempts or restart a potentially paid request. Staff/customer pages mask legacy diagnostic bodies and unsafe identifiers; search does not provide an oracle for those diagnostics.

## Owner recovery

Use an active owner account, a provider case/reference and a meaningful reason. These commands never purchase, charge, send mail or provision a domain. Do not paste credentials, contact details or provider response bodies into the reason.

```text
python manage.py reconcile_registration --attempt-id ID --owner-id ID --action adopt-legacy --remote-order-id ID --customer-id ID --provider-reference CASE --reason "Provider support confirmed the original order association"
python manage.py reconcile_registration --attempt-id ID --owner-id ID --action confirm-no-order --provider-reference CASE --reason "Provider support confirmed no order was created for this request"
```

`adopt-legacy` reads the current provider using the explicitly supplied customer ID. It requires matching order/domain/customer identity, all required role IDs, reviewed TLD policy, supported exact status, valid dates and valid nameservers. It refuses conflicting local ownership or registrar links. Owner authority and the retained historical subject are checked again under locks after the read. Only allowlisted facts, captured provider scope, actual role IDs and an audit event are saved atomically.

`confirm-no-order` records the owner's explicit provider-side investigation; it is an attestation, not API proof of absence. It refuses any positive retained registrar ID or linked domain. Only an unlinked blank/invalid-ID historical request can be released to `aborted`, with a rotated token. Invoice/payment state is unchanged; any later registration must pass the full current admission checks. Modern uncertain paid submissions cannot use this escape path.

Historical adoption records actual `Active`, `InActive` or `Suspended` facts, mapped to local states. Pending missing dates remain unknown; missing nameservers are permitted only for a pending order with no delegation. Local resume rereads provider facts, requires the retained role associations and applies actual dates, nameservers and status. Pending/suspended domains do not initiate DNS provisioning. The adopted customer ID remains authoritative if the configured default customer later changes; changing registrar environment/reseller scope blocks reuse.

## Provider operation hold

Held domains return a helpful, non-cacheable 409 before registrar changes, transfer-code disclosure, DNS read/sync/edit or renewal admission. Cached EPP codes are masked in the customer response. Customer screens explain the review and suppress renewal/DNS links; server guards remain authoritative. Turning off local auto-renew remains available. Historical notices are owner-scoped and bounded.

The same guard rejects a linked domain with inconsistent owner/name/registrar ID or a captured provider scope that differs from current configuration. Scope hashes normalize configuration the same way as the API client and contain no credentials. Verification admission/summary, renewal checkout, queued renewal work and the auto-renew scheduler enforce the hold. Checkout rechecks existing cart items before creating an invoice, including an account change after cart admission.

Older assets with no captured scope and no historical hold retain the existing legacy policy; this increment does not prove every legacy resource's provider ownership. General credential/resource binding remains part of later integration work. Direct database/server-owner intervention is outside ordinary application authorization.

## Verification

The isolated real `0016 -> 0017` migration check quarantined three representative uncertain records, including an early legacy bootstrap, and preserved a consistent completed asset and modern attempt. The final historical/provider/verification/action run passed 131 tests after the presentation refinements. The hold/action run also passed 49 tests, including the new configured-account change regression.

The whole application suite passed 1,504 tests with the unchanged excluded nginx port-80/780 assertion as its sole failure. Fourteen final desktop/mobile browser states passed contrast, accessible-name, responsive and protected-control checks; the mobile held-domain screenshot was visually inspected. Django system checks, dependency consistency and whitespace checks passed.

No production migration, registrar purchase, external message or provider mutation was performed during verification. PostgreSQL locking/process-loss behaviour and live funded provider contracts remain untested. Safe rollout requires ensuring older worker code cannot keep processing historical orders; no deployment operation is included here. Existing ordinary registration acknowledgement does not itself certify activation, and broader DNS mutation reconciliation remains outstanding.
