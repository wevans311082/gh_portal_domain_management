# Account mail delivery

Registration, address verification/resend, password-reset requests and allauth account notices use the shared durable public-delivery worker. Web handlers render bounded message templates and commit an encrypted outbox payload; they never enter SMTP/Graph. The worker supplies the notifications mailbox header, stable job Message-ID and strict no-fallback transport policy. General worker lease, preparation retry, unknown-outcome and owner-review rules are documented in [public delivery](public-delivery.md).

## Storage and key prerequisites

Token-bearing subjects, bodies, alternative HTML, addresses, template identifiers, account binding and expiry are stored together in an AES-256-GCM envelope authenticated to the delivery UUID. The envelope uses the existing external versioned integration keyring with a distinct account-mail identity. Receipt digests contain no raw mail fields. Moving an envelope to another job fails authentication. Missing/malformed keys fail closed without a synchronous/plaintext fallback. Keep old key versions available while pending mail uses them; provision keys before enabling these application changes. See [encrypted settings](encrypted-settings.md). No production key was generated or installed in this work.

Migration `core.0010_public_account_mail` adds the account receipt kind and `core.0011_submission_digest_index` indexes duplicate lookup; only isolated test databases have been migrated. Ciphertext remains in queued/failed/unknown jobs for recovery; sent/cancelled payloads are cleared. Historical retention/key retirement remain operator responsibilities. This feature does not make a database dump containing unrelated historical plaintext safe.

## Bounds and retry semantics

- Serialized message contents are capped at 256 KiB; subjects at 255 characters. Account mail accepts one recipient, no CC/BCC or attachments. Domain context queries take at most 50 domain rows and 50 service rows per field.
- Identical rendered messages/account state deduplicate while work is outstanding, including across minute boundaries, and for at least 60 seconds after creation of a recent copy. A later deliberate resend can create another job after completion and cooldown, subject to request/mail budgets. Failed/unknown copies require review before an identical copy can be queued. Concurrent writers either observe the existing receipt or return a safe retry after rollback.
- Account mail shares the global 1,000 pending/review-job admission gate. Queue or encryption failure leaves no partial receipt/job. Broker publication happens after commit and failed publication leaves recoverable queued work. Eager mode cannot send from HTTP.
- A queued message expires after 15 minutes, independent of the underlying link's validity. A delayed worker cancels it instead of delivering an old link. Links retain allauth's normal token checks; customers can request fresh instructions. Deletion, deactivation, removal of the recipient from the account or changed session-authentication state also cancels account-bound queued mail.
- SMTP/Graph sends retain the worker's bounded timeouts. A possibly accepted send becomes unknown and is never automatically repeated through another transport. No owner action reveals decrypted mail content.

The application requires an asynchronous worker for delivery. Worker/server/deployment configuration was not changed or started here. A stopped worker means mail stays queued and eventually expires; queued UI wording does not assert delivery. The custom registration path can retain a created account if queueing fails and explicitly tells the visitor to sign in later to request confirmation. It does not show a raw transport/key error or pretend the mail was delivered.

## Endpoint inventory

Early bounds cover both custom/allauth registration and login aliases, reset requests and token/code completion, MFA verification, recent-identity confirmation, address-management/resend and email confirmation. Optional login-code request/confirmation routes have explicit policies too; no social provider or passkey-signup feature is configured. Allauth's own challenge/token/rate controls remain active. Case-insensitive reset lookup reads at most two accounts; ambiguous legacy case variants fail without fanning out messages. Public contact, quotes, acceptance/invoice copies, PDFs and domain search/WHOIS use the separately documented controls.

Provider callbacks/webhooks are machine protocols with their own signature/replay handling; they are not reclassified as public forms or passed through form-body parsing. Authenticated hosting/domain/support mutations retain their authorization/provider controls and remain subject to their roadmap feature work.

## Verification

`tests/test_account_mail_delivery.py` covers no-send HTTP behavior, encrypted persistence, deduplication/resends/concurrency, message caps, authenticated UUID binding, key rotation/missing keys, expiry/state invalidation, generic HTTP failure, registration recovery, ambiguous addresses and uncertain transport. Existing account validation/outbound tests explicitly invoke the isolated worker before asserting delivered contents and EmailLog records. Test settings use a public fixture encryption key and local in-memory mail; no real mailbox/provider is contacted. Desktop/mobile reset/verification status screens are included in `scripts/verify_authentication_ui.py`.
