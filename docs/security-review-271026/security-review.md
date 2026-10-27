# Security review

**Application:** Cyber Ask Domains  
**Review date:** 27 October 2026  
**Review type:** Source-assisted application and deployment review  
**Overall risk:** Critical until SR-001 is fixed

## Executive summary

The application has several good baseline controls: object ownership is normally enforced in customer views, payment webhooks verify provider signatures, CSRF protection is retained for browser actions, secrets are redacted in admin displays, API keys are stored as hashes, archive extraction guards against path traversal, and production settings define secure cookies, HSTS, TLS redirection, CSP, and clickjacking protection.

The strongest finding is a confirmed MFA bypass. The normal settings load both the custom MFA-aware authentication backend and django-allauth's password backend. When the custom backend rejects a correct password because a TOTP token is absent, Django continues to allauth, which accepts the password and returns the user. A dynamic test reproduced a complete login without MFA.

The administrative authorization model also treats every `is_staff` account as a platform owner. A staff account can promote itself or another account to superuser, reset another user's password or MFA, impersonate customers, export integration secrets, change infrastructure endpoints, and edit the IP allowlist. This makes a single lower-trust staff compromise equivalent to full platform compromise.

The Swarm deployment compounds those risks by explicitly running the development settings module under Django's development server. It therefore omits the secure-cookie, HSTS, HTTPS-redirect, and trusted-proxy settings defined in `production.py`.

| ID | Severity | Finding |
|---|---:|---|
| SR-001 | Critical | MFA can be bypassed by the allauth authentication backend |
| SR-002 | High | Any staff account can obtain full platform control |
| SR-003 | High | The production Swarm stack runs development settings and `runserver` |
| SR-004 | High | Core dependencies are obsolete and contain known vulnerabilities |
| SR-005 | High | Client-IP trust makes rate limits and the staff IP allowlist unreliable |
| SR-006 | High | Integration secrets are duplicated in plaintext database and `.env` storage |
| SR-007 | Moderate | Public quote and contact endpoints permit resource and email abuse |
| SR-008 | Moderate | Stored-XSS paths and unrestricted same-origin logo uploads exist |
| SR-009 | Moderate | Configuration-backup import is unbounded and accepts arbitrary keys |
| SR-010 | Low | The public health endpoint returns internal exception details |

## Scope and method

The review covered:

- Django authentication, MFA, registration, password reset, session handling, impersonation, and staff authorization.
- Customer object ownership for domains, DNS, invoices, payments, support, hosting, API keys, quotes, and templates.
- Stripe and GoCardless webhook authentication and idempotency.
- Integration clients for WHM/cPanel, ResellerClub, Cloudflare, Companies House, Microsoft Graph, SMTP, and Stripe.
- File upload, media delivery, template extraction and preview, PDF generation, configuration import/export, and public form handling.
- Security middleware, proxy headers, CSP, CSRF, cookie settings, logging, Docker, Swarm, nginx, and dependency pins.
- Targeted dynamic reproduction of the MFA path, Django checks where the environment allowed, and the existing test suite results from the current working tree.

The review did not include live infrastructure inspection, cloud/IAM policy review, network scanning, third-party account review, or destructive exploit testing. `pip-audit` was not installed in the project environment, so dependency conclusions combine the exact pins with upstream security notices rather than a complete package-advisory scan.

## Findings

### SR-001 — MFA can be bypassed by the allauth authentication backend

**Severity:** Critical  
**Category:** Broken authentication / CWE-288  
**Affected code:** `cyberask_domains/settings/base.py:142-145`, `apps/accounts/views.py:181`, `cyberask_domains/settings/test.py:27-30`

The normal settings enable two authentication backends in order:

1. `apps.accounts.backends.EmailBackend`, which returns `None` for an MFA-enabled account when no TOTP is supplied.
2. `allauth.account.auth_backends.AuthenticationBackend`, which validates the same email and password without checking the application's `mfa_enabled` state.

Django tries the second backend after the first returns `None`. `custom_login()` interprets any returned user as fully authenticated and calls `login()` immediately.

The test configuration contains an explicit warning — “allauth's backend bypasses MFA” — and removes that backend, meaning the tests do not exercise the production backend list.

**Dynamic confirmation:** An in-memory database was migrated, an MFA-enabled user was created, and the login view was called with the base-settings backend list. The response was `302 /portal/`; `_auth_user_id` was populated and `_mfa_pending_user_id` was absent. No TOTP was provided.

**Impact:** Anyone who obtains a valid password for an MFA-enabled customer, staff member, or superuser can sign in without the second factor. For staff accounts, SR-002 turns this into full application and infrastructure-secret compromise.

**Fix:**

1. Remove `allauth.account.auth_backends.AuthenticationBackend` from `AUTHENTICATION_BACKENDS`; use only the custom MFA-aware backend for password authentication.
2. If allauth must remain an authentication backend, subclass it and enforce the same MFA state before returning a user. A separate “password verified but MFA pending” result should not be represented by a normal authenticated `User` object.
3. Add a regression test using the exact production backend list. Assert that correct email/password for an MFA-enabled account redirects only to the MFA step and does not set `_auth_user_id`.
4. Test all login entry points supplied by allauth, including any email-confirmation or social-account flow that can establish a session.
5. After deploying the fix, invalidate existing staff sessions and review recent staff login/audit history for password-only access to MFA-enabled accounts.

### SR-002 — Any staff account can obtain full platform control

**Severity:** High  
**Category:** Improper authorization / CWE-269  
**Affected code:** `apps/admin_tools/decorators.py`, `apps/admin_tools/forms.py:92-99`, `apps/admin_tools/forms.py:214-224`, `apps/admin_tools/content_views.py:35-150`, `apps/admin_tools/content_views.py:193-258`, `apps/admin_tools/content_views.py:782-817`, `apps/admin_tools/views.py:1986-2084`

Admin Tools uses a single `is_staff` gate for both routine operations and owner-level security functions. A staff user can:

- Create or edit users with `is_staff` and `is_superuser` fields.
- Set another user's password, validate their email, activate the account, disable/reset MFA, and generate replacement backup codes.
- Impersonate any user.
- Edit infrastructure endpoints and credentials, and test outbound connections.
- Export all runtime integration settings, including secrets.
- Modify or remove the staff IP allowlist.

There are no `is_superuser`, group, or object-permission checks around these operations.

**Impact:** A compromised support/content/operations staff account becomes a superuser and gains customer access, payment/infrastructure credentials, DNS/hosting control, and the ability to weaken other security controls. Audit logging records the requests but does not prevent the escalation.

**Fix:**

1. Define roles or Django permissions for customer support, billing, content, domain operations, integration administration, user security, and platform ownership.
2. Restrict superuser assignment, staff-role changes, password/MFA reset, impersonation, secret import/export, integration configuration, and IP-allowlist changes to active superusers or a small owner group.
3. Prevent users from increasing their own privilege, editing their own security role, impersonating staff/superusers, or resetting owner MFA without a second approver.
4. Require recent re-authentication with MFA for secret export, impersonation, password/MFA resets, and role changes.
5. Add negative authorization tests for ordinary staff against every sensitive endpoint.

### SR-003 — The production Swarm stack runs development settings and `runserver`

**Severity:** High  
**Category:** Security misconfiguration / CWE-16  
**Affected code:** `docker-stack.yml:40-50`, `cyberask_domains/settings/development.py`, `cyberask_domains/settings/production.py`

The file described as the Swarm stack pins `DJANGO_SETTINGS_MODULE=cyberask_domains.settings.development` and starts `python manage.py runserver 0.0.0.0:7000`. Setting `DJANGO_DEBUG=False` does not load the security controls in `production.py`.

The omitted controls include secure session and CSRF cookies, HSTS, forced HTTPS, and `SECURE_PROXY_SSL_HEADER`. Django's development server is also not intended as a production WSGI server. `Dockerfile.prod` correctly sets production settings, but the Swarm stack uses `cyberask-domains:local` and overrides the command.

**Impact:** Session and CSRF cookies may be issued without the `Secure` attribute, HTTPS enforcement and HSTS are absent at the application layer, proxy scheme detection can be wrong, and the deployment uses a server without production hardening or predictable concurrency behavior.

**Fix:**

1. Build and deploy `Dockerfile.prod` with an immutable versioned image.
2. Set `DJANGO_SETTINGS_MODULE=cyberask_domains.settings.production` for web, worker, and beat services.
3. Run Gunicorn, for example `gunicorn cyberask_domains.wsgi:application --bind 0.0.0.0:7000 --workers <n> --timeout <n>`.
4. Add a deployment smoke test that rejects non-production settings and checks `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT`, HSTS, and proxy headers.
5. Remove default database credentials from deployable manifests and inject unique secrets through Swarm secrets or the platform secret store.

### SR-004 — Core dependencies are obsolete and contain known vulnerabilities

**Severity:** High  
**Category:** Vulnerable and outdated components / CWE-1104  
**Affected code:** `requirements/base.txt`

Examples from the pinned runtime:

- Django is pinned to `5.1.4`. Django 5.1 is unsupported; its final version was 5.1.15 and extended support ended on 3 December 2025. Django 5.1.4 predates multiple security releases, including CVE-2025-26699. The current supported LTS listed by Django is 5.2.18.
- Pillow is pinned to `10.3.0`. Pillow's 12.2.0 security notes state that CVE-2026-40192 was introduced in 10.3.0, and list additional issues affecting versions at or after 10.3.0, including an out-of-bounds write in crafted PSD handling.
- Requests 2.31.0, cryptography 42.0.5, Gunicorn 21.2.0, Stripe 8.8.0, and several infrastructure packages are also years behind current releases. They require an advisory-driven compatibility review.

**Impact:** Known framework denial-of-service flaws are present, Django receives no further fixes on this branch, and image processing can expose memory-safety or resource-exhaustion flaws if attacker-controlled images reach Pillow now or in future features.

**Fix:**

1. Upgrade Django to the latest 5.2 LTS patch, currently 5.2.18, then run the full test suite and resolve deprecations.
2. Upgrade Pillow to a currently supported release containing the cited fixes; verify image/PDF workflows.
3. Run `pip-audit` against the locked production environment and triage every result, including transitive dependencies.
4. Add automated dependency updates and a CI gate for known high/critical vulnerabilities.
5. Rebuild containers regularly so base-image and OS fixes are included; pin images by maintained version and optionally digest.

Upstream references:

- Django supported versions: https://www.djangoproject.com/download/
- Django CVE-2025-26699 release: https://www.djangoproject.com/weblog/2025/mar/06/security-releases/
- Pillow 12.2.0 security fixes: https://pillow.readthedocs.io/en/stable/releasenotes/12.2.0.html

### SR-005 — Client-IP trust makes rate limits and the staff IP allowlist unreliable

**Severity:** High  
**Category:** Improper trust boundary / CWE-345  
**Affected code:** `apps/core/http.py:4-16`, `apps/accounts/views.py:42-46`, `apps/audit/middleware.py:71-98`, `nginx/nginx.conf:23`, `docker-compose.yml:71-72`

`get_client_ip()` always trusts `X-Forwarded-For` and returns its final entry. nginx uses `$proxy_add_x_forwarded_for`, which appends its socket peer. In the normal proxy chain the returned value is therefore commonly the proxy address, not the originating client.

This value controls login throttling and the staff IP allowlist. Consequences include:

- All proxied clients can share one rate-limit bucket, so one attacker can lock out other users.
- The allowlist can see only the proxy address; allowing that address effectively allows every client behind it.
- The lab Compose file publishes port 7000 on all interfaces. A direct client can supply its own `X-Forwarded-For`, because the application does not verify that the socket peer is a trusted proxy.
- Other code uses the first forwarded value directly, creating inconsistent and spoofable audit data.

**Fix:**

1. Define the exact trusted proxy chain and derive the client address only when `REMOTE_ADDR` belongs to that allowlist.
2. At the public edge, overwrite rather than preserve untrusted forwarding headers. Pass a single normalized client-IP header from the trusted proxy, or use a well-tested proxy-aware library configured with the exact proxy count/ranges.
3. Do not publish the application server publicly; bind direct development access to loopback only and expose production solely through the edge proxy.
4. Rate-limit login by normalized client IP plus normalized account identifier, with a separate global safeguard that does not enable trivial account lockout.
5. Add proxy-chain tests for direct, nginx-only, and Traefik-to-nginx requests.

### SR-006 — Integration secrets are duplicated in plaintext database and `.env` storage

**Severity:** High  
**Category:** Cleartext storage of sensitive information / CWE-312  
**Affected code:** `apps/admin_tools/models.py:11-39`, `apps/admin_tools/wizard_views.py:34-74`, `apps/admin_tools/wizard_views.py:635-659`, `apps/admin_tools/views.py:2044-2084`

`IntegrationSetting.value` is a plain text database field. The `is_secret` flag affects presentation but does not encrypt the value. The setup wizard writes the same credentials into `.env` and the database. Any staff account can export all database-held settings in an offline password-protected archive.

**Impact:** Database read access alone exposes Stripe, registrar, WHM root, Cloudflare, email, and other credentials. Filesystem or backup compromise exposes a second copy. The broad staff authorization in SR-002 increases the likelihood and blast radius.

**Fix:**

1. Store secrets in a platform secret manager (Swarm/Kubernetes secrets, Vault, or a cloud KMS-backed service) and store only references in the database.
2. If runtime database storage is essential, use authenticated envelope encryption with a master key held outside the database. Include key versioning and rotation.
3. Stop rewriting `.env` from web requests. Treat deployment configuration as immutable and apply changes through a controlled deployment path.
4. Restrict secret configuration and export to owner-level users with recent MFA re-authentication; log fields changed without logging values.
5. Migrate existing values, securely remove redundant copies, and rotate every credential that was stored in plaintext.

### SR-007 — Public quote and contact endpoints permit resource and email abuse

**Severity:** Moderate  
**Category:** Uncontrolled resource consumption / CWE-400; HTML injection / CWE-79  
**Affected code:** `apps/billing/public_views.py:81-159`, `apps/core/views.py:111-197`

The anonymous quote endpoint relies only on a honeypot. It has no per-client/account rate limit, request-body limit, maximum line count, quantity ceiling, or field-length validation before creating database records and calling `email_document()`. PDF rendering and outbound email make each accepted request comparatively expensive.

The contact form also has no rate limit. It interpolates name, email, phone, subject, and message directly into an HTML email body. An attacker can inject HTML into messages viewed by staff, subject to the receiving mail client's own sanitization.

**Impact:** Attackers can generate database growth, PDF/CPU load, outbound-email cost and reputation damage, staff inbox flooding, and potentially deceptive active content in staff email clients.

**Fix:**

1. Add shared-cache rate limits by verified client IP and normalized email, with conservative endpoint and global ceilings.
2. Enforce request-size limits at the edge and application layer; cap line count, quantity, text lengths, and total quote value.
3. Queue public quote email/PDF work with idempotency keys and bounded retries rather than performing it during the request.
4. Escape all user-controlled values in HTML mail with Django's escaping utilities, or build the message with a template that autoescapes.
5. Add bot protection after threshold breaches, and monitor submission/email volume.

### SR-008 — Stored-XSS paths and unrestricted same-origin logo uploads exist

**Severity:** Moderate  
**Category:** Stored cross-site scripting / CWE-79; unrestricted upload / CWE-434  
**Affected code:** `templates/public/quote_builder.html:145`, `apps/billing/public_views.py:75`, `apps/website_templates/views.py:137-145`, `apps/billing/models.py:41`, `apps/admin_tools/billing_views.py:155-156`, `nginx/nginx.conf:17-19`

Three related content paths are unsafe:

- The public quote builder embeds `json.dumps(catalogue)` with `|safe` inside a `<script>` element. A product description containing `</script>` can break out into executable markup.
- The public thumbnail endpoint interpolates a database template name into SVG/XML without escaping and serves it as `image/svg+xml` on the application origin.
- Billing branding accepts an arbitrary uploaded file as a logo without validating its content or extension. nginx serves `/media/` on the application origin. An uploaded HTML or active SVG file can become same-origin active content when opened directly.

These writes currently require staff access, which reduces likelihood but does not make same-origin executable content safe—especially given SR-001 and SR-002.

**Fix:**

1. Replace the quote JSON block with Django's `json_script` filter and parse the generated element by ID.
2. Escape XML text in generated SVG, or replace generated SVG responses with a static raster/placeholder implementation. Add `nosniff` and a restrictive CSP.
3. Validate logo files by decoding them with Pillow, enforce dimensions and size, re-encode to PNG/WebP, and reject SVG/HTML and extension/content mismatches.
4. Serve user media from a separate cookieless origin with a restrictive CSP and `X-Content-Type-Options: nosniff`; use attachment disposition for non-image downloads.
5. Add stored-XSS tests using `</script>`, SVG event handlers, and misleading file extensions.

### SR-009 — Configuration-backup import is unbounded and accepts arbitrary keys

**Severity:** Moderate  
**Category:** Uncontrolled resource consumption / CWE-409; improper input validation / CWE-20  
**Affected code:** `apps/admin_tools/views.py:2065-2084`, `apps/admin_tools/config_backup.py`

The import view calls `upload.read()` without checking upload size. The parser opens a caller-supplied zip and reads `payload.bin` without compressed/uncompressed size or ratio limits. After decryption, it imports every non-empty key, with no allowlist, count limit, value limit, or atomic transaction.

The endpoint is staff-only, but SR-002 means a routine staff compromise can use it to consume memory/CPU or inject unexpected runtime keys. Partial imports can remain if a later row fails.

**Fix:**

1. Reject uploads over a small fixed size before reading them.
2. Require an exact archive structure and enforce entry count, compressed size, uncompressed size, and compression-ratio limits before extraction/read.
3. Allowlist supported setting keys and validate each value using the same forms used for interactive configuration.
4. Cap row count and value length, reject duplicate keys, and import within `transaction.atomic()`.
5. Restrict the endpoint to owner-level users with recent MFA re-authentication.

### SR-010 — The public health endpoint returns internal exception details

**Severity:** Low  
**Category:** Information exposure / CWE-209  
**Affected code:** `apps/core/views.py:8-25`

When database or cache checks fail, the unauthenticated endpoint returns `str(e)` in JSON. Driver and connection errors can reveal hostnames, ports, database names, Redis topology, or operational details useful for follow-on attacks.

**Fix:** Return only stable external states such as `ok` or `error`; log detailed exceptions server-side with a correlation ID. Consider separate liveness and readiness routes, and limit detailed readiness output to trusted monitoring networks.

## Positive controls observed

- Customer-facing queries generally scope objects to `request.user`, including domains, DNS records, invoices, saved cards, support tickets, hosting services, and API keys.
- Stripe uses the provider's signed-event construction path, and GoCardless uses HMAC with `compare_digest`; both reject missing/invalid secrets.
- Webhook event IDs are stored and checked for idempotency.
- Browser state changes generally use POST and retain Django CSRF protection; webhook exemptions are paired with signature verification.
- Password creation uses Django validators; passwords are hashed by Django.
- API keys use cryptographically random generation and persist only a SHA-256 hash and short prefix.
- Public quote tokens use UUID4 values and ownership is checked when an already-linked quote is accepted.
- Support attachments have a 5 MB limit and extension allowlist, and nginx denies direct `/media/support/` access.
- Website-template file serving resolves paths and checks containment; archive extraction also checks destination containment.
- Production settings define secure cookies, HSTS, TLS redirect, no-sniff, clickjacking protection, and CSP—the primary problem is that the Swarm stack does not load them.
- Runtime settings displayed in Admin Tools redact flagged secrets.
- The application maintains audit records for authenticated state-changing requests.

## Review limitations

- No live environment, network boundary, TLS configuration, database permissions, Redis authentication, secret-store ACLs, email tenant, Stripe/Cloudflare/WHM account, or backup system was inspected.
- No dynamic attack was made against production. The only exploit reproduction used an in-memory local database and test client.
- The dependency review is not a complete software-composition analysis. Run `pip-audit` and container-image scanning after upgrading and locking dependencies.
- The working tree already contained broad admin-theme edits. This review did not treat those changes as a clean baseline and did not alter application code.

