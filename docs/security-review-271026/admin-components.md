# Admin components and accessibility

Roadmap item 1 builds on the existing admin theme. Semantic tokens, reusable templates and shared behavior live in `static/css/admin.css`, `templates/admin_tools/components/`, `apps/admin_tools/templatetags/admin_components.py` and `static/js/admin-components.js`. It does not change route authorization, provider operations or deployment configuration.

## Component contracts

| Component | Parameters / slots | Production use |
|---|---|---|
| Page header | `heading`, optional `description`, `action_url` and `action_label` | Customer, invoice and staff-role pages |
| Card | `card_heading`, optional description; extend `card_body` | Staff-role editor and current access |
| Statistic card | `label`, `value`, optional `prefix` and `detail` | Customer and invoice summaries |
| Action bar | `action_bar_label`; extend `actions` | Customer search, owner-only WHM sync and creation |
| Table | `table_label`; extend `table_head` and `table_body` | Customer and invoice lists |
| Pagination | `{% load admin_components %}` then `{% admin_pagination page_obj %}` | Shared `_pagination.html` and both migrated lists |
| Form field | Bound `field`, including prefixed forms and grouped choices | User editor and staff-role editor |
| Error summary | Bound `form` | User editor and staff-role editor |
| Empty state | Optional heading/message/action | Empty customer and invoice searches |
| Status | `label`; tone is success, warning, danger, info or neutral | Invoice payment states |
| Loading | Unique `loading_id`, optional message | Pending WHM sync request |

Labels, headings and messages are escaped. Status tones map to a fixed class allowlist; an unknown value uses neutral styling. Action URLs must come from trusted view/reversed-route values, and actions stay inside their existing permission checks. Components never grant access or call providers. Trusted form definitions own help text; request values must not be supplied as help markup.

For a table, extend the table template in a small partial and fill its two blocks; include that partial from the page. Column headers use `scope="col"`, identifying customer/invoice cells use `scope="row"`, and repeated invoice links have document-specific accessible names. The scroll region is named, focusable and positioned so its screen-reader caption stays inside its containing block. Wide tables scroll horizontally without widening the document; keyboard users can focus the region and use arrow keys.

Pagination copies the request QueryDict, replaces all existing page parameters with one page number, and URL-encodes filters including repeated values. HTML escaping remains active. A one-page result has no unnecessary pagination controls.

Fields use real labels, required indicators, Django's error IDs and help associations. Grouped widgets use fieldsets/legends and a separate focus target; summary links target the field or group. Hidden-field errors are reported without an unfocusable link. A returned error summary receives focus; following a summary link explicitly focuses the relevant control/group. This behavior is opt-in through the shared component, not an attempt to rewrite every custom form with JavaScript.

## Drawer behavior

The mobile drawer becomes a labelled modal dialog while open. It focuses the close button, wraps Tab/Shift+Tab among visible controls, makes background content inert and locks background scrolling. Escape, close-button and backdrop dismissal restore focus to the opener and restore the previous scroll/inert state. Navigation links close the drawer as navigation proceeds. A viewport change removes modal state on desktop and recovers keyboard focus when the desktop navigation becomes a closed mobile drawer. Desktop navigation remains nonmodal.

Drawer and backdrop stack above the shared cookie banner and toasts. A ResizeObserver reserves space for the visible cookie banner in the admin scroll container; the reserve disappears when consent hides the banner. Loading feedback scrolls into the unobscured area. The CSS also hides a closed mobile drawer before Alpine initializes. Existing reduced-motion handling is preserved, including a nonanimated loading indicator when reduced motion is requested.

## Pending operations

Only an explicitly marked full-page form uses the shared loading handler:

```html
<form method="post" data-admin-submit="operation-progress">
  <!-- CSRF, inputs and existing action controls -->
</form>
```

Include the loading component outside that form with `loading_id="operation-progress"`. Native validation runs before submit. The handler shows an announced status and marks the form busy, blocks a repeated submit, and retains named submit controls so the action still reaches the server. `pageshow` resets pending state after back/forward restoration. This is presentation and accidental-double-click prevention; server-side idempotency and authorization remain necessary for each operation. HTMX/fetch forms require their own request lifecycle and must not adopt this full-page hook without handling completion/error reset.

## Verification and limits

`tests/test_admin_components.py` verifies encoded multi-value pagination, escaped content, allowed status classes, prefixed field/group error associations, hidden errors and normal-text contrast of the actual semantic CSS tokens (at least 4.5:1). Browser checks verify the rendered cascade and behavior rather than relying only on token values.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_admin_components.py -q -p no:cacheprovider
.venv/Scripts/python.exe scripts/verify_authentication_ui.py --axe "$env:TEMP/admin-theme-axe.min.js"
.venv/Scripts/python.exe scripts/verify_authentication_ui.py --axe "$env:TEMP/admin-theme-axe.min.js" --pages admin-customers admin-customers-loading admin-user-invalid admin-invoices
```

Browser fixtures use real Django-rendered pages, isolated SQLite and installed Chrome at widths 1440 and 390. Populated and empty customer/invoice lists, a pending action, invalid forms and open/closed drawer states are exercised. The pending-form fixture uses an isolated frame target and intercepts its real native POST to inspect loading behavior and action serialization without starting WHM work. Accessibility checks cover contrast, accessible names, labels/ARIA, focus loops/recovery and document overflow. Representative screenshots are inspected separately. Screenshots/results are under the system temporary `portal-authentication-ui` directory; fixture serving does not replay production CSP headers.

This is representative coverage and a reusable foundation. Bespoke workflows in later roadmap items must use these contracts and add their own state coverage. It is not a claim that every historic admin template, browser, assistive technology or live provider operation has been fully audited. Final checkpoint results are recorded in [implementation status](implementation-status.md).
