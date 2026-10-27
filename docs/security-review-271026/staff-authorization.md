# Staff authorization — SEC-02

## Role and capability matrix

An owner is an active account with both `is_staff` and `is_superuser`. Owner status is never inferred from a group name. All operational roles require an active staff account. Customers cannot use Admin Tools even if assigned a permission accidentally.

| Role | Customers | Billing | Domains | Hosting | Support | Content | Audit |
|---|---|---|---|---|---|---|---|
| Support | Read | — | Read | Read | Manage | — | — |
| Billing | Read | Manage | — | — | — | — | — |
| Content | — | — | — | — | — | Manage | — |
| Domain Operations | Read | — | Manage | — | — | — | — |
| Hosting Operations | Read | — | — | Manage | — | — | — |
| Auditor | Read | Read | Read | Read | Read | — | Read |
| Platform owner | Manage | Manage | Manage | Manage | Manage | Manage | Read |

Groups can be combined. Read does not permit POST. Manage is bounded by the route and field restrictions below; it does not grant security administration. A staff member without a role sees a minimal landing page and cannot access operational lists. The executable matrix is `apps/admin_tools/access.py`; a route-inventory system check prevents newly added routes from silently inheriting broad staff access.

## Route families

| Routes / family | Read requirement | Write requirement | Additional boundary |
|---|---|---|---|
| Staff landing | Active staff | Denied | Only permitted navigation; owner statistics are omitted for operational staff |
| Customer list, company lookup | View customers | Denied | Customer list POST synchronization is owner + recent MFA; privileged accounts are omitted for ordinary staff |
| Invoice/quote/payment lists and document PDFs | View billing | Denied | Document mutation uses separate management routes |
| Invoice/quote/payment editors, cart builder and billing branding | Manage billing | Manage billing | Existing method and business validation still applies |
| Domain and order lists | View domains | Denied | No domain-management authority implied |
| Domain-order detail | View domains | Manage domains | Writes also require recent MFA; ownership, price, status and registrar identity fields are owner-only |
| Domain editors, registration, order actions and pricing | Manage domains | Manage domains | Recent MFA required; adding invoice/hosting requires the corresponding management capability; mark-paid/retry actions additionally require billing |
| Hosting service list | View services | Denied | — |
| Hosting service editors | Manage services | Manage services | Provisioning identity/configuration and ownership reassignment are owner-only |
| Ticket/contact lists | View support | Denied | — |
| Ticket/contact editors | Manage support | Manage support | Existing method restrictions still apply |
| Content lists | View content | Denied | — |
| Content editors | Manage content | Manage content | Catalogue pricing, active state, provider mapping and provisioning configuration are owner-only |
| Audit log | View audit | Denied | — |
| Staff roles, account management, MFA administration, impersonation start, statistics, tasks, integration configuration, diagnostics, settings, database tools, imports/exports, allowlists, email templates/logs, webhook logs, flags, promotions and setup wizard | Platform owner | Platform owner | Recent MFA required |
| Native Django admin | Platform owner | Platform owner | Recent MFA required, including custom admin URLs; own privilege fields read-only and own deletion forbidden |
| Stop impersonation | Valid recorded owner context | Valid recorded owner context | Uses validated restoration rather than the impersonated customer's permissions |

The source policy enumerates every URL name, rather than matching URL prefixes. Tests compare that enumeration to the actual URL configuration and issue denied GET/POST requests against every ordinary operational/owner route as unassigned staff.

## Recent identity confirmation

The session proof is bound to the authenticated user's ID and current credential fingerprint and expires after five minutes. Future, invalid, stale or mismatched proofs are rejected. Password changes, MFA changes and security-flag changes invalidate session security state. Confirmation requires the current password and a fresh TOTP or unused backup code, uses shared account attempt limits, and rotates the session key.

A rejected sensitive POST has no side effects. The confirmation page explains that the user must return and submit the action again; request bodies and secrets are not stored for replay. Return destinations are restricted to the current host. Unenrolled owners are directed to MFA enrollment. HTML and binary Admin Tools responses prohibit caching.

## Assignment and impersonation

Role assignment replaces only predefined portal groups, preserves other groups, requires a reason, records before/after groups in the audit transaction and rotates the target's session version. Owners and inactive/non-staff accounts cannot be selected. Removing a group takes effect on the next request even if changed outside this form because permissions are checked per request.

Impersonation stores the actor, target, start time, owner session hash and reason after a mandatory audit write. It is limited to active non-staff/non-owner customers and 30 minutes. Owner deactivation, demotion, password/MFA/security-state changes or target privilege changes terminate the context. Customer credential management is blocked through both custom and allauth paths; API key changes, data exports and saved-card changes are blocked. Customer write audit entries include the impersonator ID. Stop restores only the validated owner and deliberately discards recent-MFA proof.

## Migration and verification

Apply the normal application migrations to create the permission definitions and six groups. No existing staff member receives access automatically. A verified owner then assigns roles from Staff roles and access. Group migration rollback preserves assignments rather than deleting potentially used groups. Existing customer data was not migrated during development.

Run `tests/test_staff_authorization.py`, `tests/test_security_authentication.py`, the full application suite, Django system checks and `scripts/verify_authentication_ui.py`. See [implementation status](implementation-status.md) for the measured results and the separate pre-existing nginx test failure. Secret encryption, trusted-IP handling, bounded imports and other remaining workstreams are not closed by permission enforcement.
