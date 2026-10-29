# Saved list views ? verified application acceptance

Roadmap item 4 is verified against its original requirements for customers, domains, billing invoices and tickets. The full 20-feature and nine-security-workstream roadmap remains active. No provider call, production migration or deployment change was performed.

## Delivered behavior

Staff can load, save, replace by exact name and delete personal named views. Filters, selected columns, sort and page size are stored per staff member. Apply settings updates the displayed table; Save personal view explicitly stores those applied settings. Reset returns to the default table while retaining named views. Named views survive sign-out; selected settings can also be carried in the page URL. No automatic default-selection preference is claimed.

Built-in views include active/inactive customers, domains expiring today through 30 days inclusively, open tickets, and overdue invoices. Overdue requires a past due date, an unpaid/overdue/partially-paid state and a positive outstanding balance; draft, void, paid and zero-balance invoices are excluded. Filters intersect the existing authorized query, never replace it. The existing customer privileged-account exclusion is retained.

The same explicit field projection drives table cells and CSV data. Exports contain the selected columns and the whole filtered result in the applied sort order, regardless of the current pagination page. Exactly 1,000 data rows are allowed; larger results are refused with a recovery link to narrow filters rather than silently truncated. CSV values that could become spreadsheet formulas are neutralized. Responses use no-store and downloads use nosniff.

Native labelled controls and disclosures work without custom JavaScript. Table regions support keyboard scrolling. Identifiers and amounts remain together on narrow screens. Read-only record links remain available; edit/delete and owner customer actions retain their route permissions. Invalid settings or export limits render an accessible recovery page. Valid request settings are retained in the return URL when possible; invalid requests do not alter stored views.

## Acceptance matrix

| Requirement | Authoritative evidence | Result |
|---|---|---|
| Named filters, columns, sorting and page size per staff member | `StaffListView`, additive migration 0006, validated persistence service and actual HTTP save/load tests; cross-owner/table lookup and deletion denial. | Verified |
| Four initial lists and built-in views | Four existing list routes use `prepare_list`; rendered-column tests cover each resource. Query tests prove expiry date boundaries, overdue balances/states and open-ticket membership. | Verified |
| Survive sign-out and reset | HTTP save, logout, login and load/export test; reset retains named records and deletion is separate. Exact-name replacement and 20-view bound tests pass. | Verified |
| Validate fields and filters | Declared columns/sorts/presets, typed page sizes and resource status/active enums; bounded names/search; control-character, duplicate-column, repeated-setting and arbitrary ORM-field rejection. Stored JSON is revalidated. | Verified |
| Cannot bypass server permissions | Route plus dynamic resource capability checks; fresh active/staff check on save; cross-owner denial, role revocation, privileged-account export exclusion and staff permission inventory tests. | Verified |
| Export matches visible filter and safe limits | HTTP pagination/export comparison proves identical filtered ordering and selected columns. Exactly 1,000 rows passes, 1,001 produces no download and a working recovery link. CSV formula protection tests pass. | Verified |
| Styled, accessible and usable | Real Chrome/axe checks cover desktop/mobile settings, native keyboard operation, contrast, names, page overflow and error recovery. Mobile settings, invoice table and save-error screenshots were visually inspected. | Verified |

## Implementation and safeguards

- `apps/admin_tools/list_preferences.py`: owner-scoped persistence and typed configuration. Owner-row locking serializes the per-table save limit; a database constraint enforces unique owner/resource/name.
- `list_queries.py`: authorized-query refinement, deterministic sort with PK tie breaker, explicit projections and bounded CSV export.
- `list_view_controls.py` and `saved_view_views.py`: request parsing, pagination, private personal controls and safe recovery. POST has CSRF, an early 16 KiB body limit and shared-cache throttling.
- Shared controls/table/action templates replace repeated table plumbing while retaining capability-checked source actions. Company and invoice-source data remain available as columns; private note bodies, credentials and raw provider payloads are not export fields.
- Migration `admin_tools.0006_staff_list_views` is additive and infers no preferences. No production migration has been applied.

## Final verification ? 29 October 2026

- **40 focused preference/query/HTTP tests pass.** Tests are in `test_list_preferences.py`, `test_list_queries.py` and `test_saved_view_routes.py`.
- **2,340 application tests pass**, zero failures/errors; the unchanged nginx port assertion is deselected because deployment changes are excluded. [Machine-readable JUnit](verification/item-4-junit.xml), with optional local hostname removed and no passing-test logs captured.
- **12 final desktop/mobile browser states pass** for the four saved tables and two recovery screens. Earlier 16-state checks also covered existing customer populated/empty/loading/Support variants. Representative evidence: [mobile table](verification/item-4-invoices-mobile.png), [recovery](verification/item-4-save-error-mobile.png).
- `scripts/verify_saved_view_migration.py` passes the isolated in-memory 0005-to-0006 upgrade, preserving historical staff and notes with zero inferred views.
- Django system check reports no issues; diff whitespace check passes. Existing database-at-app-initialization and staticfiles warnings remain.
- Global migration dry run still exits nonzero for previously documented unrelated WizardProgress Meta, DomainOrder status choices, support attachment and website-template field drift. No saved-view model drift is reported and no unrelated migrations were generated.

These checks prove application behavior under controlled test data, not production infrastructure readiness. Continue with roadmap item 5; external gates already recorded for other workstreams remain open.
