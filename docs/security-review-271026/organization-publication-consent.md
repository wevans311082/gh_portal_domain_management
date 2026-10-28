# Organization publication consent

This increment implements explicit per-request organization publication choices for roadmap item 3. It does not establish identity, business verification, registrar email verification or registry eligibility. Individuals still need no company number. Item 3 and the full roadmap remain in progress.

## Provider contract

ResellerClub documents `registrant-org-consent` as a true/false attribute when a gTLD request contains an organization name. Its guidance says organization names are redacted by default and an organization provided as registrant is the legal holder. See the provider's [registration contract](https://manage.resellerclub.com/kb/answer/752), [transfer contract](https://manage.resellerclub.com/kb/answer/758) and [organization publication guidance](https://manage.resellerclub.com/kb/node/3489), checked 28 October 2026. This implementation uses the current reviewed .com/.net/.org policies; .io and UK policies do not acquire an inferred gTLD publication capability.

The client emits `attr-name1=registrant-org-consent` with a literal `true` or `false` for those reviewed generic extensions, including the safe default false. Other extensions omit the attribute and reject an affirmative publication value. Direct transport values must be actual booleans, preventing the string `false` from becoming truthy. This attribute is separate from purchasing privacy protection.

## Explicit capture and payment checks

Registration and shop registration/transfer forms default to **Keep organization redacted**. An explicit **Publish organization for this request** choice is valid only for an organization contact on a reviewed generic extension. The disclosure explains public WHOIS/RDAP visibility, authority to consent, the organization as holder, and the distinction from verification/privacy. Individual opt-in, unsupported extensions, invalid values and duplicate submitted publication fields are rejected. Staff-created requests retain the no-consent default; staff cannot infer customer opt-in.

`RegistrantPublicationDecision` retains the customer, contact, domain, contact-data digest, disclosure version, creation time and later withdrawal state. Audit metadata records the choice/version and decision reference, without copying contact details. Capture rereads and locks the active ordinary customer and owned contact, and refuses a contact changed since the caller's reviewed snapshot. It cannot silently authorize publication of a newly edited organization.

The cart item, registration order or transfer holds a protected decision reference. Cart checkout rechecks the evidence before allocating an invoice, including public quote conversion through the shared preflight. A changed contact, customer, domain or disclosure version cannot authorize publication. Missing consent means false, never inferred true. Registration invoice/order/decision creation is atomic. Posted registration identity now comes from the POST body consistently, preventing a conflicting query-string domain from selecting another domain's price or consent context.

Paid registration and transfer preparation and the final HTTP transaction recheck the current evidence and freeze its safe metadata in the attempt subject. Relevant contact edits or withdrawals during preparation invalidate the original snapshot before the paid request. Withdrawal after a paid marker cannot make an uncertain paid request retryable. Acknowledged local handoff still reuses its original order without another purchase; changing local consent does not pretend to reverse an earlier provider action.

Consent digests are separate from identity/business/email verification evidence. A publication choice does not mutate a shared remote contact. Historical migrations `domains.0019` and `portal.0004` add nullable references and create no affirmative decisions for existing records. The real isolated upgrade check verifies old registration, transfer and cart records remain null and no consent is inferred. The existing DomainOrder status-choice drift is excluded from these feature migrations.

## Withdrawal and existing orders

Customers can review their own decisions, 20 per page, from My domains and withdraw by CSRF-protected POST. The list is private/non-cacheable and cannot expose another customer's records or digests. Repeated withdrawal is idempotent and the audit commits atomically.

For requests that have not reached the provider, withdrawn evidence now supplies false. A preparation using the earlier true snapshot must stop and pass fresh admission checks. A changed cart item can also be removed and re-added with redaction or a fresh explicit choice; the cart explains this.

If a retained registration/transfer may already have reached the registrar, withdrawal creates one high-priority support ticket and an explanatory message in the same transaction. The customer is directed to that request. The interface explicitly states that local withdrawal is not confirmation of public provider redaction. It never automatically purchases, unlocks, changes contacts, bypasses IRTP or sends an external message. The support operator must confirm the original provider association and apply/verify redaction through the provider workflow. Automated provider redaction and its reconciliation remain part of the remaining contact-change workflow; do not close item 3 on this increment alone.

## Verification

The focused publication suite passes 37 tests. A combined publication/cart/registration/transfer run passed 111 tests before the final two capture-race regressions. The final broad application run passed 1,613 tests with only the unchanged excluded nginx port-80/780 assertion. Django, dependency consistency and whitespace checks pass.

Sixteen desktop/mobile browser states cover registration, shop, cart, stale consent, empty/active/withdrawn decisions and provider-processing support links. Checks include contrast/names, overflow, default redaction, unique shop field IDs, real POST/CSRF controls and withdrawal state. Representative mobile consent and registration screenshots were inspected; meaningless generic field hints and redundant form-card styling were removed. Shop field labels and low-contrast actions were corrected as part of verifying the new consent paths.

No production migration, provider mutation, payment or external message was performed. Tests use isolated SQLite and mocked providers. PostgreSQL locking/process-loss behaviour and live provider contract execution remain external verification tasks. Remaining item 3 work includes eligibility adapters and contact replacement/IRTP, with provider redaction confirmed through the tracked support workflow until its automation is implemented.
