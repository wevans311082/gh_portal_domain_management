# Permission-aware global search

Roadmap item 2 adds a search entry in the admin header/navigation and one GET search page. It searches live customer, domain, hosting-service, invoice, quote and support-ticket records. It neither calls providers nor copies sensitive source models into a separate search service.

## Matching and bounds

Search accepts 2-255 characters and rejects control characters. The interface explicitly describes **prefix matching**, rather than promising arbitrary substring/full-text search. Wildcards are literal characters. Keys include customer email/first/last/full name, domain name, service domain/cPanel domain, invoice/quote number, anonymous quote lead email/name and numeric ticket references (`#123` or `123`). Ticket subjects, messages, internal notes, credentials and arbitrary model fields are not search keys.

Actors with customer access can also find owned assets/documents/tickets by a matching customer name or email. This relationship lookup is disabled without customer permission, so counts cannot be used to probe a restricted customer name. Staff/owner accounts are excluded from the customer group and relationship lookup. Inactive customer records remain available to authorized staff. Other source groups use the existing global staff capability scope; this is not a customer-facing or tenant-sharing endpoint.

The overview shows at most five matches per permitted group. Selecting a group gives stable, 20-row pages and at most 500 matching records. A capped count is displayed as `500+`, with guidance to narrow the query. Counts themselves use sliced count queries; page offsets are bounded/clamped. This bounds returned/count-preview data, not the absolute execution time of every query on an arbitrarily large database. Native datalist suggestions return at most two identifiers per permitted group (12 overall), without private snippets, record contents or totals. The complete GET search works without the enhancement.

Search/suggestions share the early request policy: 2,048-character query-string bound, no request body, 120 requests per IP/minute, 240 per authenticated account/minute and 1,000 globally/minute. Counter failure denies work. Browser responses and JSON prohibit caching; permission state is checked again on the next request. The client debounces suggestions, aborts superseded requests, ignores stale responses and constructs option text/values without HTML insertion. A failed suggestion request preserves input and offers normal form submission.

## Authorization and safe record links

All three routes require active staff and at least one existing search-group capability. Unassigned staff, ordinary customers, anonymous callers and content-only roles cannot use search. Every group has its own capability check before count/query/suggestion work; an inaccessible or unknown group/detail returns a generic 404. Existing role/session revocation remains effective. The permission inventory explicitly covers the new routes.

| Group | Required capability | Read-only fields |
|---|---|---|
| Customers | view_customers | Email, name, active status |
| Domains | view_domains | Domain, customer email, status, expiry, auto-renew |
| Hosting services | view_services | Domain, customer email, status, next due date |
| Invoices | view_billing | Number, customer email, status, currency/total |
| Quotes | view_billing | Number, customer/lead email, status, currency/total |
| Tickets | view_support | Reference, customer email, subject, status, priority |

Search links open a dedicated, read-only record view with a fresh capability and source-object check. This gives auditors/read-only staff usable links without granting access to edit forms. Detail queries select only required fields. A separate manage link appears only when the actor may access the existing manage route; its original recent-MFA/write controls still apply. Internal notes, ticket messages/attachments, MFA/password fields, EPP codes and provisioning settings are never serialized into results or details. Financial details/counts are absent for actors without billing access. HTML is escaped; JSON is data, and client code never treats suggestions as HTML.

## Indexes and database behavior

Eleven expression indexes cover the normalized customer keys, domain/service names and invoice/quote identifiers. Foreign-key indexes support customer-to-source relationships. Numeric ticket prefixes use bounded ranges over the existing primary-key index, avoiding a cast/subject scan. Query predicates and migrations share `SearchKey`/`FullName` expressions; fixed full-name separator literals let SQLite recognize the same indexed expression. Database-maintained indexes reflect ordinary saves, bulk updates and deletes immediately, without a stale application projection.

PostgreSQL uses native LOWER with C-collated comparison/index keys. Query normalization uses the same database case conversion as the stored expression. SQLite uses the deterministic Unicode-aware `PORTAL_LOWER` function registered on each Django connection, with binary ordering; this avoids SQLite's ASCII-only built-in lowercasing for names such as Émile/Müller. Neither backend promises accent removal/transliteration or all Unicode case-fold equivalences. Changing normalization semantics requires a corresponding index rebuild/migration. SQLite writes must use a connection with the application function registered, as Django's application connections do.

Migrations are accounts `0004_search_indexes`, domains `0007_search_indexes`, services `0005_search_indexes`, and billing `0008_search_indexes`/`0009_search_indexes`. Only isolated test databases were migrated. Applying/reviewing index creation against production volume remains part of the separately excluded deployment process. No PostgreSQL extension, external search engine, server process or infrastructure configuration is required by these application changes.

## Verification

`tests/test_admin_search.py` covers exact/prefix/Unicode matching, numeric references and overflow, literal wildcard/injection text, result caps/pagination, role filtering, no billing-table queries for support, inaccessible categories/records, customer-name probing without customer permission, permission revocation, updates/deletes, manage-link gating, shared throttles/cache failure and HTML/JSON safety. SQLite EXPLAIN checks verify use of the real expression indexes. PostgreSQL SQL compilation verifies collation/full-name syntax and parameterization while explicitly forbidding a server connection; this is not a PostgreSQL runtime/query-plan test.

The expanded browser checker covers grouped/empty/invalid/paginated search, role-limited results, read-only details, long identifiers, suggestion loading/success/failure, retained input/focus, keyboard form submission order and desktop/mobile contrast/names/overflow. Representative screenshots are visually inspected. The fixture suggestion payload comes from the real permission-filtered Django endpoint, while network timing/failure is simulated locally. Tests do not contact providers or claim live PostgreSQL/Redis/assistive-technology verification. Exact final counts and existing baseline failures are in [implementation status](implementation-status.md).

```powershell
.venv/Scripts/python.exe -m pytest tests/test_admin_search.py tests/test_staff_authorization.py -q -p no:cacheprovider
.venv/Scripts/python.exe scripts/verify_authentication_ui.py --axe "$env:TEMP/admin-theme-axe.min.js"
```
