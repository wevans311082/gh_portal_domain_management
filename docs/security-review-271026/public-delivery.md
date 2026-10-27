# Public submission and delivery contract — SEC-06 in progress

This records the implemented receipt/outbox stage. SEC-06 remains in progress: PDF subprocess bounds and complete public error/retry UX still require completion. Domain input/fan-out bounds are covered by the later checkpoint in implementation status. This is not a deployment approval or a claim that external workers/providers were tested.

## Submission identity

Contact and quote forms receive a random UUID key and a session owner marker before submission. One database transaction creates the unique receipt, application record and at most one delivery job. No mail/provider call occurs in that transaction. After commit, publication attempts enqueue the persisted job. Publication failure leaves recoverable queued work.

The same explicit key and payload return the same result only within the original owner scope. Reusing it with changed data or another browser returns 409 without the original private result. Explicit receipt identity is independent of Django signing-key rotation: changed bindings may deny replay, but cannot create a second document for that UUID. Legacy clients without a key deduplicate identical data within their session; they should retain cookies or supply an explicit UUID for reliable retries. A new form/key means a new intentional submission.

Request-size/rate limits still apply to retries. Owner/payload digests are HMAC-protected; opaque UUID receipt keys contain no contact fields. Submission records retain result identifiers for replay, not full raw request bodies. Repeated delivery messages cannot create additional quote documents.

## Delivery states

| State | Meaning | Recovery |
|---|---|---|
| Queued | Persisted and waiting for a worker | Publish again if a broker message expires/is lost |
| Preparing | Worker holds a lease and builds the message/document | Retry only after lease expiry; maximum three automatic attempts |
| Delivering | Send boundary persisted before external transport | Never automatically repeat if interrupted |
| Sent | Transport accepted the message, or owner confirmed provider evidence | No resend; payload cleared |
| Failed | Preparation exhausted its retry budget | Owner may approve a new preparation cycle with a reason |
| Unknown | Provider may have accepted a send; outcome needs review | Owner confirms from evidence or closes without another send |
| Cancelled | Suppressed by preference or explicitly closed | No send; payload cleared |

“Sent” is transport acknowledgment, not proof that the recipient read or received the message in their inbox. Exactly-once SMTP delivery is not claimed. Stable Message-ID values help correlate provider evidence; they do not guarantee that providers deduplicate mail.

Workers use a compare-and-set lease transition before sending. An old preparer whose lease was reclaimed cannot send. A duplicate broker task skips a job already claimed/sent/uncertain. If the worker sends but loses its database connection before recording success, the persisted sending state later becomes unknown rather than causing a second send.

Public messages prohibit the normal Microsoft Graph-to-SMTP fallback after a Graph error, because that can duplicate an already accepted message. Public Graph HTTP timeouts are capped at 10 seconds and SMTP uses a 15-second timeout. Celery task soft/hard limits are declared at 35/45 seconds; dispatcher limits are 20/30 seconds. Native renderer child-process bounds are still outstanding and must not be inferred from worker limits.

## Capacity and scheduling

An application admission gate locks one row before counting queue/review work. At 1000 queued, preparing, delivering, failed or unknown jobs, new delivery-requiring submissions fail without creating a quote/contact/job. Existing receipt replay still works. Database-only contacts do not consume delivery capacity.

The application beat schedule includes a one-minute dispatcher. Each tick handles stale leases and publishes at most 100 due jobs. Automatic preparation attempts use bounded backoff. A broker outage does not lose accepted jobs. Eager Celery configuration does not execute public delivery in a web callback; it leaves jobs queued and is reported by the deployment security check. Tests invoke workers explicitly with fixture messages.

No broker, beat process, worker, network or server configuration was changed or launched during implementation. The application scheduling entry requires the normal existing Celery/beat runtime to be operating. Actual pool time-limit behaviour and live provider paths remain an external verification scope.

## Owner review

Task Management requires a platform owner and recent MFA. Its delivery panel shows review, queued and active work, the oldest 25 matching rows, lifetime attempts and provider Message-ID references. A reason/evidence field is required for changes; the action and before/after states are audited atomically.

Owners can retry preparation failures, confirm an uncertain send from provider evidence, or close queued/failed/unknown work without sending. Unknown sends cannot be blindly retried, and in-progress sends cannot be falsely cancelled. Closing or confirming clears retained delivery payload. Retrying preparation retains lifetime attempts while starting a fresh bounded automatic cycle.

Do not treat an unknown outcome as proof of failure. Check provider records for the stable message reference before confirming or closing. These application controls do not reconcile live provider logs automatically.

## Data and verification boundaries

Contact HTML uses an autoescaping template. Email-only submissions do not create a staff contact record; their pending job carries delivery data until successful/suppressed/closed. Database/both destinations preserve the normal staff enquiry record. Job errors use generic codes and do not store exception strings or submitted message content in routine logs.

The test suite covers transaction rollback, duplicate ownership, concurrent receipts, safe contention retries, queue admission, broker failure, leases, uncertain delivery, owner audit rollback and transport fallback prevention. Browser checks cover populated and invalid owner review screens at mobile/desktop widths. See [implementation status](implementation-status.md) for exact results and remaining requirements. PostgreSQL/Redis multi-worker execution, live mail, native PDF renderer execution and historical-data retention review are not established by these isolated tests.


## PDF resource contract

Combined HTML input (body, header and footer) is limited to 1 MiB. Optional PDF engines share a 15-second execution deadline; process creation and bounded cleanup can add overhead. Each subprocess captures at most 10 MiB of PDF and 64 KiB of diagnostics. Only output starting with the PDF signature is accepted. Timeout or output overflow stops further engine attempts and uses the existing HTML fallback. Oversized HTML fails preparation before starting a process and follows the bounded preparation retry policy.

Processes run without a shell or application credentials in their environment. Windows Job Objects and POSIX process groups support descendant cleanup. The WeasyPrint worker is a separate protocol process without Django/database initialization. These are resource controls, not a security sandbox or memory isolation. The unpatched pdfkit advisory remains documented in [dependency updates](dependency-updates.md).

Windows subprocess behavior and controlled renderer adapters are tested; native engine visual output and POSIX execution remain unverified. See [implementation status](implementation-status.md) for current results.


## Public failure presentation

Boundary failures preserve HTTP error statuses. Browser document requests receive a standalone recovery page without database context processors; JSON quote callers retain JSON. Retry-After is mirrored as retry_after for quote/HTMX clients. HTMX renders errors using textContent in its existing result target and preserves keyboard focus and the search input. Quote failures keep cart/contact values and their submission key for a deliberate retry; normal request budgets still apply. Recovery pages offer fresh GET navigation and browser-Back guidance, without replaying a POST or displaying submitted secrets. Responses prohibit caching and vary on Accept/HX-Request.


## Quote acceptance and invoice copies

Returning from sign-in opens a read-only confirmation page. Only a CSRF-protected POST may finalize acceptance. Ownership is checked before revealing a previously converted invoice. The quote claim, invoice conversion and one invoice-kind receipt/outbox row commit together; capacity/conversion failure rolls them back. A same-account replay across browser sessions returns the original invoice and does not queue another copy. The worker prepares the invoice through the existing bounded PDF/email pipeline and cancels preparation if the customer account is inactive. The ordinary uncertain-send recovery rules apply to invoice copies too. Allauth account mail now uses encrypted, expiring payloads in this outbox too; see [account mail delivery](account-mail-delivery.md).
