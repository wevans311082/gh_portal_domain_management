# Remediation plan

This order addresses exploitability first and then reduces the blast radius of future compromise.

## Immediate — same day

### 1. Close the MFA bypass (SR-001)

- Remove the allauth password backend from the production backend list or replace it with an MFA-aware implementation.
- Add a regression test using the production backend configuration.
- Exercise every login/session-creation route.
- Deploy, invalidate staff sessions, and review recent MFA-enabled staff logins.

**Acceptance criteria:** A correct password without TOTP or a valid unused backup code never creates `_auth_user_id` for an MFA-enabled user. This remains true for the custom login, allauth routes, admin login, confirmation links, and social login if enabled.

### 2. Put the Swarm deployment on production settings (SR-003)

- Deploy the `Dockerfile.prod` image.
- Use `cyberask_domains.settings.production` and Gunicorn.
- Remove the hard-coded database password and inject a rotated secret.
- Verify secure cookies, HTTPS redirects, HSTS, proxy scheme handling, and host validation from outside the cluster.

**Acceptance criteria:** A deployment smoke test fails when development settings or `runserver` is detected. Public responses demonstrate the intended security headers, and cookies carry `Secure`, `HttpOnly`, and the chosen `SameSite` policy.

### 3. Upgrade vulnerable dependencies (SR-004)

- Move Django to the latest 5.2 LTS patch and Pillow to a supported fixed release.
- Run the test suite, `pip-audit`, and an image scanner.
- Rebuild from a current base image.

**Acceptance criteria:** Production uses a supported Django release; no known high/critical dependency finding is accepted without a documented owner, compensating control, and expiry date.

## Urgent — within 7 days

### 4. Split administrative privileges (SR-002)

- Create permissions for support, billing, content, domain operations, security administration, integration administration, and platform ownership.
- Gate security-sensitive endpoints on explicit permissions and recent MFA.
- Block self-promotion and staff-to-owner changes without an owner/dual-approval path.

**Acceptance criteria:** A normal staff fixture receives 403 responses for superuser changes, password/MFA reset, impersonation, secret export/import, integration credentials, and IP-allowlist modification. Each role has positive and negative endpoint tests.

### 5. Correct client-IP handling (SR-005)

- Document the Traefik → nginx → Django chain and trusted network ranges.
- Normalize forwarding headers only at the trusted edge and reject/overwrite client-supplied versions.
- Remove public access to the application-server port.
- Update login throttling and IP-allowlist tests.

**Acceptance criteria:** Tests produce the same correct client IP for direct and documented proxy-chain requests; spoofed headers from an untrusted peer do not change it. The application server is unreachable from the public network.

### 6. Protect stored integration credentials (SR-006)

- Choose a secret manager or KMS-backed envelope-encryption design.
- Migrate database and `.env` secrets.
- Rotate Stripe, WHM, registrar, Cloudflare, email, payment, and Microsoft credentials.
- Remove runtime `.env` writes from HTTP handlers.

**Acceptance criteria:** A database dump and application-media/filesystem backup do not disclose usable integration credentials. Secret access is owner-only and audited without values.

## Near term — within 30 days

### 7. Add public endpoint abuse controls (SR-007)

- Apply shared Redis rate limits and request/body/field limits to quote, contact, registration, password reset, domain search, and other public expensive operations.
- Escape HTML email values.
- Queue PDF/email generation with idempotency and bounded retries.
- Add monitoring and alert thresholds.

**Acceptance criteria:** Burst and sustained abuse tests receive 429 responses without creating unbounded rows or email jobs. HTML payloads are rendered as text in staff email.

### 8. Remove same-origin active-content paths (SR-008)

- Use `json_script` for catalogue data.
- Escape or rasterize generated thumbnails.
- Decode and re-encode logo uploads; reject active formats.
- Move user media to a separate origin and add content headers.

**Acceptance criteria:** The stored-XSS payload corpus cannot execute in the quote builder, thumbnail route, or uploaded media. Uploaded non-images and image/polyglot files are rejected.

### 9. Bound and validate configuration imports (SR-009)

- Add upload and archive-expansion limits.
- Enforce a setting-key allowlist and typed validation.
- Make imports atomic.

**Acceptance criteria:** Oversized, high-ratio, malformed, duplicate-key, unknown-key, and partially invalid backups are rejected without changing stored settings.

### 10. Reduce operational disclosure (SR-010)

- Return generic health states and log detailed failures internally.
- Restrict detailed readiness checks to monitoring infrastructure.

**Acceptance criteria:** Anonymous callers cannot obtain exception messages, hostnames, ports, database names, cache endpoints, or stack details from health responses.

## Ongoing controls

- Run unit/integration tests, `manage.py check --deploy`, `pip-audit`, secret scanning, static analysis, and container scanning in CI.
- Add an authorization matrix and security regression tests to code review requirements.
- Review staff accounts, roles, MFA status, API keys, integration credentials, and audit logs quarterly.
- Test restore procedures and credential rotation, not only backup creation.
- Commission a focused external penetration test after SR-001 through SR-006 are remediated.

