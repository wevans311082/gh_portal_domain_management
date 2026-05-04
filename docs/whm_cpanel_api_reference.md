# WHM & cPanel API Reference (Project Notes)

> **Audience:** developers and AI agents working on this project.
> **Status:** Working reference. Items marked **VERIFIED LIVE** have been
> tested against a real WHM/cPanel server. Items marked **PER DOCS** come
> from the official cPanel/WHM documentation and have not yet been
> integration-tested in this codebase.
>
> **Why this file exists:** to stop ourselves and AI agents fabricating
> endpoints, parameter names, or response shapes. Always update this file
> when the live server proves something different from what's written here.

Official upstream documentation:

- WHM API 1: <https://api.docs.cpanel.net/whm/introduction/>
- cPanel UAPI (preferred): <https://api.docs.cpanel.net/openapi/cpanel/operation/uapi-introduction/>
- cPanel API 2 (legacy, still supported): <https://api.docs.cpanel.net/openapi/cpanel/operation/api2-introduction/>

When in doubt, the upstream docs are the source of truth — but our notes
below capture the *gotchas*, conventions, and project-specific decisions
that aren't obvious from the docs alone.

---

## 1. Servers, Ports, and URL Schemes

There is **one underlying daemon (`cpsrvd`)** but the URL/port chosen
determines which API surface (and which user) you hit.

| Service | Port | TLS | Typical user | Use for |
|---|---|---|---|---|
| WHM (HTTPS) | **2087** | yes | `root` / reseller | Server-wide ops: create accounts, suspend, list packages |
| WHM (HTTP)  | 2086 | no | `root` / reseller | Avoid in production |
| cPanel (HTTPS) | **2083** | yes | end-user (account) | Per-account ops: email, MySQL, FTP, files |
| cPanel (HTTP) | 2082 | no | end-user | Avoid in production |
| Webmail (HTTPS) | 2096 | yes | mailbox user | Webmail logins |

**Rule of thumb:**

- *Reseller / server admin actions* → WHM API 1 on port 2087.
- *End-user account actions* → cPanel UAPI on port 2083, authenticating
  as the cPanel user (or impersonating via WHM session token).

> **Pitfall:** WHM API endpoints called on port 2083 will fail with a
> permissions or "unknown function" error and vice versa. Always match
> port to API.

---

## 2. Authentication

There are **three** supported credentials. Pick one — do not mix.

### 2.1 API Token (recommended, current)

Created via WHM → "Manage API Tokens" or `whmapi1 api_token_create`.
Token replaces the password in the `Authorization` header. Tokens can be
scoped (ACLs) and revoked individually — much safer than the legacy
access hash.

**Header format (WHM):**

```http
Authorization: whm root:THEAPITOKENVALUE
```

**Header format (cPanel):**

```http
Authorization: cpanel cpaneluser:THEAPITOKENVALUE
```

Notes:

- The keyword is literally `whm` or `cpanel`, lowercase, then a space,
  then `user:token`.
- No `Basic ` prefix, no base64 encoding. Plain text after the keyword.
- Always send over HTTPS.

### 2.2 Access Hash (legacy)

A long multi-line hash from `/root/.accesshash`. Joined into a single
line (strip newlines) and used in the same header slot:

```http
Authorization: WHM root:HASHWITHNONEWLINES
```

This still works on current versions but is deprecated; new code should
use API tokens.

### 2.3 Basic Auth (password)

```http
Authorization: Basic base64(user:password)
```

Works but discouraged: passwords can change, can't be scoped, and get
logged in more places. Acceptable for short-lived scripts only.

### 2.4 cPanel session via WHM (impersonation)

For server admins to access a user's cPanel without their password:

```
GET https://server:2087/json-api/create_user_session
    ?api.version=1
    &user=cpaneluser
    &service=cpaneld
```

Returns a one-time URL that drops you into that user's cPanel. Combine
with API token auth on the WHM call.

---

## 3. Request Rules (read this before writing code)

Common mistakes our agents and the upstream community have made:

1. **Parameters are query string OR form-encoded body, not JSON.**
   Both WHM API 1 and cPanel UAPI accept `application/x-www-form-urlencoded`
   POST bodies and GET query strings. Sending `Content-Type: application/json`
   with a JSON body will be ignored or rejected. Use `requests`' `params=`
   for GET and `data=` for POST.
2. **Repeated parameters for arrays.** When an endpoint accepts a list
   (rare, but happens), pass repeated keys, not comma-joined values:
   `?domain=a.com&domain=b.com`, not `?domain=a.com,b.com`. The
   `requests` library handles this when you pass a list value.
3. **Always include `api.version=1`** on WHM API 1 calls. Without it
   you'll get the legacy XML-API response shape.
4. **JSON output format** is selected by the URL path:
   - WHM JSON: `https://host:2087/json-api/<function>`
   - WHM XML:  `https://host:2087/xml-api/<function>`
   - cPanel UAPI JSON: `https://host:2083/execute/<Module>/<function>`
   - cPanel API 2 JSON: `https://host:2083/json-api/cpanel?cpanel_jsonapi_apiversion=2&cpanel_jsonapi_module=<M>&cpanel_jsonapi_func=<F>`
   Use the JSON paths. Never parse the HTML responses.
5. **Self-signed certificates.** Out-of-the-box WHM uses a self-signed
   cert. In dev you may set `verify=False` — but only with a
   `urllib3.disable_warnings()` shim, and never in production. Production
   should use a real cert (Let's Encrypt via WHM's AutoSSL).
6. **Rate limits exist but are not documented.** Empirically,
   `cpsrvd` will start returning 503s if you hammer it. Cache aggressively
   and batch where possible.
7. **Username constraints.** cPanel usernames are 1–16 chars, lowercase
   alphanumeric, must start with a letter, no underscores. Some old
   docs say 8 — that limit was raised. Always trim/validate before
   sending.
8. **Domain must be the *primary* domain on `createacct`.** Addon
   domains are added separately via `Park::park` (UAPI) or
   `addaddondomain` (API 2).

---

## 4. Response Shapes

### 4.1 WHM API 1 (JSON)

Every successful response is wrapped:

```json
{
  "metadata": {
    "version": 1,
    "command": "createacct",
    "reason": "Account Creation Ok",
    "result": 1,
    "output": { "raw": "Account Creation Complete!!!..." }
  },
  "data": { /* function-specific payload */ }
}
```

- `metadata.result == 1` → success
- `metadata.result == 0` → failure; read `metadata.reason`
- HTTP status will usually be **200 even on logical failure**. Don't
  rely on it. Always check `metadata.result`.

### 4.2 cPanel UAPI

```json
{
  "status": 1,
  "errors": null,
  "warnings": null,
  "messages": null,
  "data": { /* payload */ },
  "metadata": { "transformed": 1 }
}
```

- `status == 1` → success
- `status == 0` → failure; `errors` is an array of strings.

### 4.3 cPanel API 2 (legacy)

```json
{
  "cpanelresult": {
    "apiversion": 2,
    "module": "Email",
    "func": "addpop",
    "data": [ { "result": 1, "reason": "..." } ]
  }
}
```

- Always inside `cpanelresult`.
- `data` is **always an array**, even for single-item operations.

---

## 5. WHM API 1 – Common Endpoints

Base URL: `https://<server>:2087/json-api/`

| Function | Purpose | Key params |
|---|---|---|
| `createacct` | Create a hosting account | `username`, `domain`, `password`, `plan`, `contactemail`, `quota` |
| `removeacct` | Terminate an account | `username`, `keepdns` (0/1) |
| `suspendacct` | Suspend (disable) | `user`, `reason`, `disallowun` (0/1) |
| `unsuspendacct` | Re-enable | `user` |
| `listaccts` | List accounts | `search`, `searchtype` (`user`/`domain`/`owner`), `searchmethod` (`exact`/`regex`) |
| `accountsummary` | Single account details | `user` *or* `domain` (one of) |
| `changepackage` | Move account to a different package | `user`, `pkg` |
| `passwd` | Reset cPanel password | `user`, `password`, `db_pass_update` (0/1) |
| `modifyacct` | Modify account quotas/owner | `user`, plus fields to change |
| `listpkgs` | List packages | (none) |
| `addpkg` / `killpkg` / `editpkg` | CRUD packages | `name`, `quota`, `bwlimit`, etc. |
| `applist` | List service status (apache, named, ...) | (none) |
| `restartservice` | Restart a service | `service` (e.g. `httpd`, `dovecot`) |
| `version` | WHM version | (none) — useful for health checks |
| `api_token_create` / `api_token_revoke` / `api_token_list` | Manage tokens | `token_name`, ACLs |

**Example: create an account**

```http
GET /json-api/createacct
    ?api.version=1
    &username=acmeco
    &domain=acmeco.example
    &password=GeneratedSecurePass!
    &plan=BasicPlan
    &contactemail=billing@acmeco.example
    &quota=5000
HTTP/1.1
Host: server:2087
Authorization: whm root:TOKEN
```

Pitfalls:

- `quota` is in **MB**. `0` means unlimited (only allowed if package
  permits unlimited).
- `password` must satisfy WHM's password strength (configurable in
  Tweak Settings). On rejection, `metadata.reason` will say
  *"Sorry, the password you selected cannot be used..."*.
- `username` collisions return result 0 with a clear reason — handle as
  a normal validation error, not an exception.

**Example: list accounts (paged-style)**

```http
GET /json-api/listaccts?api.version=1&search=acmeco&searchtype=user
```

Returns `data.acct` as an array of account objects. Field names of
note: `user`, `domain`, `email`, `plan`, `suspended` (0/1), `disk_used`,
`disklimit`, `unix_startdate`.

---

## 6. cPanel UAPI – Common Endpoints

Base URL: `https://<server>:2083/execute/<Module>/<function>`
Auth as the cPanel user (token or password). UAPI is the **preferred**
modern API; use it instead of API 2 unless the function only exists in
API 2.

| Module / Function | Purpose | Key params |
|---|---|---|
| `Email/add_pop` | Create a mailbox | `email` (local part), `password`, `domain`, `quota` (MB) |
| `Email/delete_pop` | Delete mailbox | `email`, `domain` |
| `Email/list_pops` | List mailboxes | (none) |
| `Email/passwd_pop` | Change mailbox password | `email`, `password`, `domain` |
| `Mysql/create_database` | New DB | `name` |
| `Mysql/create_user` | New DB user | `name`, `password` |
| `Mysql/set_privileges_on_database` | Grant | `user`, `database`, `privileges` |
| `Mysql/list_databases` | List | (none) |
| `Ftp/add_ftp` | Add FTP user | `user`, `pass`, `homedir`, `quota` (MB or 0) |
| `Ftp/delete_ftp` | Remove FTP user | `user` |
| `Ftp/list_ftp` | List | (none) |
| `Park/park` | Add addon/parked domain | `domain`, `topdomain`, `dir` |
| `Park/unpark` | Remove addon/parked | `domain` |
| `SubDomain/addsubdomain` | Create subdomain | `domain`, `rootdomain`, `dir`, `disallowdot` |
| `SSL/install_ssl` | Install cert | `domain`, `cert`, `key`, `cabundle` |
| `DomainInfo/list_domains` | List all domains on the account | (none) |
| `Quota/get_quota_info` | Disk usage | (none) |
| `StatsBar/get_stats` | Account stats (disk, bandwidth, etc.) | `display` (comma list of stat ids) |

**Example: create a mailbox**

```http
GET /execute/Email/add_pop
    ?email=info
    &password=Strong!Pass1
    &domain=acmeco.example
    &quota=250
HTTP/1.1
Host: server:2083
Authorization: cpanel acmeco:TOKEN
```

Notes:

- `email` is the **local part only**. The `@` and domain are split into
  the `domain` field. Sending `info@acmeco.example` in `email` will
  silently produce a mailbox literally named `info@acmeco.example@acmeco.example`.
- `quota` of `0` = unlimited (capped by account's mailbox quota).

---

## 7. cPanel API 2 – When You Still Need It

Some operations only exist in API 2 (e.g. some mail-forwarders, certain
Cron features). Path:

```
/json-api/cpanel
    ?cpanel_jsonapi_user=<user>
    &cpanel_jsonapi_apiversion=2
    &cpanel_jsonapi_module=<Module>
    &cpanel_jsonapi_func=<func>
    &<other args>
```

Examples in the wild:

| Module / func | Purpose |
|---|---|
| `Email/listforwards` | List mail forwarders |
| `Email/addforward` | Add a forwarder (`email`, `fwdopt=fwd`, `fwdemail`) |
| `Cron/listcron` | List cron jobs |
| `Cron/add_line` | Add a cron line |
| `MysqlFE/userdbprivs` | Get DB privileges (UAPI equivalents are split) |

Always check if a UAPI version exists first; prefer UAPI.

---

## 8. Common Workflows in This Project

### 8.1 Provision a new hosting account (Domain + cPanel + first mailbox)

1. **WHM** `createacct` (port 2087) with the chosen package.
2. Wait for `metadata.result == 1`. Re-poll `accountsummary` until the
   account is fully built (a few seconds typically).
3. **WHM** `create_user_session` if you need to redirect the customer
   to cPanel without giving them a password.
4. **cPanel UAPI** `Email/add_pop` for any default mailboxes (e.g.
   `info@`, `support@`).
5. **cPanel UAPI** `SSL/install_ssl` if shipping a custom cert; otherwise
   AutoSSL handles it on the next run.
6. Persist the account record in our own DB; treat WHM as the source
   of truth and re-sync on schedule.

### 8.2 Suspend / Unsuspend (e.g. unpaid invoice)

- Suspend: WHM `suspendacct` with `reason="Payment overdue invoice #123"`.
- Unsuspend: WHM `unsuspendacct`.
- Always log the reason in our `audit` app — WHM stores its own log too
  but ours is the customer-facing one.

### 8.3 Migrate an account between packages

- WHM `changepackage` with the new package name. Quota changes are
  applied immediately. Beware: lowering quotas while user is over them
  can cause writes to fail; check `accountsummary.disk_used` first.

### 8.4 Reset a customer password

- WHM `passwd` with `db_pass_update=1` so MySQL/FTP secondary users
  inherit the change. Do **not** use cPanel UAPI for the cPanel
  account's own password — that requires the *current* password; WHM
  `passwd` does not.

---

## 9. Configuration in This Project

Settings keys (in `apps/core/runtime_settings`):

- `WHM_HOST` – fully qualified host, no scheme, no port.
- `WHM_PORT` – default `2087`.
- `WHM_USERNAME` – usually `root` or a reseller user.
- `WHM_API_TOKEN` – the API token string (preferred).
- `WHM_VERIFY_TLS` – boolean. **Must be true in production.**
- `CPANEL_PORT` – default `2083`.

Client class location (planned): `apps/provisioning/whm_client.py`. The
class should mirror the conventions used by `ResellerClubClient`:

- One requests `Session` per instance.
- All endpoints exposed as named methods, never as raw URLs to callers.
- Always inject `api.version=1` for WHM calls.
- Always check `metadata.result` (WHM) or `status` (UAPI) and raise a
  domain-specific exception on failure with the upstream `reason`
  string.
- Cache idempotent reads (`listpkgs`, `version`) for short TTLs to
  avoid hammering the server.

---

## 10. Myth-busting (mistakes we won't repeat)

1. **"WHM uses Basic Auth."**
   It supports it but our project uses API token auth with the
   `Authorization: whm user:token` header. No base64.
2. **"You can JSON-encode the body."**
   You can't, in practice. Use form encoding or query strings. Sending
   JSON results in silently dropped parameters.
3. **"Failures show up as HTTP 4xx/5xx."**
   They almost always come back as **HTTP 200** with `metadata.result=0`.
   Always parse the body before deciding success.
4. **"`cpanel_jsonapi_user` is optional."**
   Only when the request comes from inside that user's session. For
   server-side calls authenticated as `root`, it's required, otherwise
   you'll get errors about an unknown user.
5. **"UAPI and API 2 take the same arguments."**
   They often don't. UAPI parameter names are usually clearer
   (`email`/`domain` vs API 2's combined `email=info@example.com`).
   Read the per-function docs.
6. **"WHM and cPanel share API tokens."**
   They don't. WHM tokens authenticate WHM API 1; cPanel tokens
   authenticate cPanel UAPI/API 2 *for that one cPanel user only*.
7. **"`createacct` returns the account immediately."**
   It returns when the *creation request* is accepted. The account
   may take several seconds to be fully usable. Poll `accountsummary`
   if subsequent calls need to operate on it.
8. **"Self-signed cert can be ignored forever."**
   Fine for dev, but in production we always require a real TLS cert
   (AutoSSL or external) and `WHM_VERIFY_TLS=True`.

---

## 11. Test/Health-check Endpoints

Cheap, idempotent calls suitable for monitoring:

- WHM: `GET /json-api/version?api.version=1` → returns `data.version`.
- cPanel: `GET /execute/Variables/get_user_information` → returns the
  user's basic info.

Use these for liveness probes; don't hammer `listaccts` or `listpkgs`
just to check connectivity.

---

## 12. Client Method Mapping (planned `WhmClient`)

| Method (planned) | Calls | Notes |
|---|---|---|
| `version()` | WHM `version` | Liveness check |
| `list_accounts(search=None, searchtype="user")` | WHM `listaccts` | Returns list of account dicts |
| `account_summary(user=None, domain=None)` | WHM `accountsummary` | One of `user`/`domain` required |
| `create_account(**fields)` | WHM `createacct` | Validates required fields client-side |
| `remove_account(user, keepdns=False)` | WHM `removeacct` | |
| `suspend_account(user, reason)` | WHM `suspendacct` | Always require a non-empty reason |
| `unsuspend_account(user)` | WHM `unsuspendacct` | |
| `change_package(user, package)` | WHM `changepackage` | |
| `set_password(user, password, update_db=True)` | WHM `passwd` | `db_pass_update=1` |
| `list_packages()` | WHM `listpkgs` | Cached briefly |
| `create_user_session(user, service="cpaneld")` | WHM `create_user_session` | Returns one-time URL |
| `restart_service(service)` | WHM `restartservice` | `httpd`, `dovecot`, `exim`, `mysql` |

Planned `CpanelClient` (per cPanel user):

| Method | Calls |
|---|---|
| `add_email(local, domain, password, quota_mb=0)` | UAPI `Email/add_pop` |
| `delete_email(local, domain)` | UAPI `Email/delete_pop` |
| `list_emails()` | UAPI `Email/list_pops` |
| `add_database(name)` | UAPI `Mysql/create_database` |
| `add_db_user(name, password)` | UAPI `Mysql/create_user` |
| `grant_db_user(user, database, privileges="ALL PRIVILEGES")` | UAPI `Mysql/set_privileges_on_database` |
| `add_ftp(user, password, homedir, quota_mb=0)` | UAPI `Ftp/add_ftp` |
| `add_subdomain(sub, root, dir)` | UAPI `SubDomain/addsubdomain` |
| `park_domain(domain, root, dir="")` | UAPI `Park/park` |
| `install_ssl(domain, cert, key, cabundle="")` | UAPI `SSL/install_ssl` |

Implement these only as we need them — don't ship dead code. When you
do implement one, **add a "VERIFIED LIVE" note next to its row above**
once you've called it against a real server and observed the documented
behaviour.

---

## 13. When You Discover Something New

If you (human or AI) find that the live server behaves differently
from these notes:

1. Reproduce it twice to be sure.
2. Edit the relevant section above and add **VERIFIED LIVE YYYY-MM**.
3. If it contradicts the upstream docs, also note that — cPanel docs
   occasionally lag behind the actual server behaviour.
4. Add a regression test against a recorded fixture if possible.

This file is the single place where we accumulate WHM/cPanel tribal
knowledge. Keep it honest.
