# Application dependency update — 27 October 2026

## Selected versions

Package versions were checked against PyPI metadata on 27 October 2026. Django uses the patched 5.2 LTS branch rather than a feature-series migration: [supported releases](https://www.djangoproject.com/download/) and [5.2.18 security release notes](https://docs.djangoproject.com/en/5.2/releases/5.2.18/). The other selected direct packages use their current published versions, except the explicit pdfkit exception below.

| Package | Previous pin | Updated pin |
|---|---|---|
| Django | 5.1.4 | 5.2.18 |
| psycopg2-binary | 2.9.9 | 2.9.13 |
| django-environ | 0.11.2 | 0.14.0 |
| celery | 5.3.6 | 5.6.3 |
| django-celery-beat | 2.7.0 | 2.9.0 |
| django-celery-results | 2.5.1 | 2.6.0 |
| redis | 5.0.1 | 8.1.0 |
| django-redis | 5.4.0 | 7.0.0 |
| Pillow | 10.3.0 | 13.0.0 |
| stripe | 8.8.0 | 16.0.1 |
| django-allauth | 65.3.0 | 65.21.0 |
| cryptography | 42.0.5 | 50.0.2 |
| requests | 2.31.0 | 2.34.2 |
| whitenoise | 6.6.0 | 6.12.0 |
| gunicorn | 21.2.0 | 26.2.0 |
| django-storages | 1.14.3 | 1.14.6 |
| boto3 | 1.34.82 | 1.43.119 |
| pyotp | 2.9.0 | 2.10.0 |
| qrcode | 7.4.2 | 8.2 |
| django-model-utils | 4.4.0 | 5.0.0 |
| django-extensions | 3.2.3 | 4.1 |
| factory-boy | 3.3.0 | 3.3.3 |
| pytest | 8.3.5 | 9.1.1 |
| pytest-cov | 6.1.1 | 7.1.0 |
| pytest-django | 4.11.1 | 4.14.0 |
| responses | 0.25.7 | 0.26.3 |
| django-debug-toolbar | 4.3.0 | 8.0.0 |
| sentry-sdk | 1.44.0 | 2.73.0 |

django-ratelimit remains 4.1.0, its current published release. pip-audit 2.10.1 was added to development tooling. pdfkit remains 1.0.0 under the documented unresolved exception. No optional old WeasyPrint pin is recommended by the requirements comments.

Upstream compatibility/security references reviewed include [allauth release notes](https://docs.allauth.org/en/latest/release-notes/recent.html), [Pillow 13.0.0 notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html), [cryptography changelog](https://github.com/pyca/cryptography/blob/main/CHANGELOG.rst), [Requests release history](https://requests.readthedocs.io/en/latest/community/updates/) and [pip-audit's security model](https://github.com/pypa/pip-audit#security-model). The saved scans provide the exact package/advisory IDs rather than assuming a current version is vulnerability-free.

## Application compatibility

Allauth removed the previous send_email_confirmation utility. Registration/resends now use the installed verification flow with the intended address and signup context, preserving rate-limit and message handling. Email-only configuration uses ACCOUNT_LOGIN_METHODS and ACCOUNT_SIGNUP_FIELDS. Verification mail, reset flows, custom MFA, native-admin entry and allauth session boundaries are covered by regression tests.

Stripe's SDK selects an API version at release time, so upgrading its major version can also change the wire contract. The application now explicitly uses the existing `2023-10-16` API version through a shared configuration function. This follows [Stripe's API override guidance](https://docs.stripe.com/api/versioning). Live webhook endpoint/account versions were not modified. A separate future API-contract migration must validate provider test-mode responses and webhook payloads. New HTTP-intercepted tests exercise the actual upgraded SDK rather than mocking its service methods.

## Scan results and reproduction

| Scope | Packages | Unique advisory-ID/package pairs | Skipped |
|---|---:|---:|---:|
| Previous direct pins only | 30 | 87 | 0 |
| Updated complete production resolution | 57 | 1 | 0 |
| Updated complete development resolution | 88 | 1 | 0 |

The initial scanner returned duplicate advisory records; saved normalized reports deduplicate the same package/advisory ID and preserve aliases/fix versions. Baseline direct-only results do not establish the full previous transitive vulnerability count. Updated reports do include resolved transitives. Both updated resolutions exactly match installed package versions used for the application tests. An additional local-environment scan covered 96 installed packages and found the same remaining pdfkit advisory.

```powershell
.venv/Scripts/python.exe scripts/audit_dependencies.py --profile production --output-dir docs/security-review-271026/dependency-audit/production
.venv/Scripts/python.exe scripts/audit_dependencies.py --profile development --output-dir docs/security-review-271026/dependency-audit/development
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe manage.py check --settings=cyberask_domains.settings.test
```

The audit commands resolve requirements without installing them, then scan an explicit complete pin list so nested requirement-file handling cannot accidentally omit base dependencies. They require network access to package metadata/advisories and do not use --fix or install arbitrary updates. Reports omit copied advisory prose and package download URLs. They retain findings and exit 1 while the PDF exception remains; no ignore list makes the scan appear clean. Saved pins describe these observed native-platform resolutions, not a universal hashed/cross-platform lock.

## Unresolved high-severity PDF exception

**Package:** pdfkit 1.0.0. **Advisory:** PYSEC-2026-2860 / GHSA-9g3x-6x24-vf9f / CVE-2025-26240. **Severity:** high. **Patched release:** none reported. [Advisory record](https://github.com/advisories/GHSA-9g3x-6x24-vf9f).

The upstream wrapper is [deprecated alongside wkhtmltopdf](https://github.com/JazzCore/python-pdfkit). The application now builds PDFKit commands and runs them through a bounded subprocess runner, but still enables local-file access; the advisory concerns JavaScript execution/local-file exfiltration. Billing templates currently escape ordinary fields, but that is not equivalent to fixing the renderer or proving it safe for hostile HTML. Existing runtime/OS renderer vulnerabilities were not scanned by this Python package audit.

This exception records an unresolved risk because no patched package release exists. It is not an assertion of safety or user risk acceptance. Replacing the renderer requires application output/security checks plus coordination of whichever host runtime it needs; removing the package and silently losing PDF generation would not satisfy the existing billing workflow. The package is therefore retained visibly pending replacement. Re-review by **26 November 2026**, or immediately if an exploitable input path, patch or replacement is identified. Track remediation with the application maintainer/platform owner. No advisory is suppressed by the reproducible scan.

## Verification boundaries

Full application suite: **911 passed**, with the existing nginx port-80-versus-780 assertion still failing. Representative browser checks: **24 desktop/mobile states passed**. pip dependency consistency, Django checks and whitespace checks pass. The regression suite exercises the updated image sanitizer, encrypted settings, backup cryptography, email/MFA integration and payment/webhook logic in isolated fixtures.

Tests ran on Windows/Python 3.13 with SQLite. Linux/PostgreSQL, external service test-mode exchanges and real worker/server/renderer execution are not claimed verified. No Docker, Swarm, nginx, network, OS or deployment configuration was changed. The Python package pins include gunicorn but no server was launched or reconfigured. These limits and the PDF exception remain explicit when assessing the historical dependency finding.


## Contact-code dataset dependencies - 28 October 2026

Added pycountry 26.2.16 and phonenumbers 9.0.42 for offline assigned country/calling-code validation. Published wheels were installed into the existing local virtual environment; pip check passes. The packages add no transitive requirements in the observed resolution. Full production/development resolutions and advisory scans were rerun, saved in dependency-audit/contact-validation-production and dependency-audit/contact-validation-development. They cover 59 and 90 packages respectively, with zero skips. Neither added package has a reported advisory in these scans; the same pdfkit advisory/aliases remain visible and the audit exits nonzero for that existing exception. A transient PyPI metadata response interrupted the first production resolution; its completed retry produced the saved report. No package exceptions were hidden or deployment changes performed.

## Offline DNS record planning - 30 October 2026

Added `dnspython==2.9.0` for typed RDATA parsing and canonical wire comparisons. The planner uses no resolver or network queries. This package adds no dependencies without optional extras. A targeted `pip-audit` scan of this exact added pin reports no known vulnerabilities; the retained input and JSON result are in [dependency-audit/dns-record-planning](dependency-audit/dns-record-planning/). This is a scan of the new dependency, not a renewed clean audit of the entire application. The existing PDF exception remains unresolved.

Reproduce the targeted scan with `.venv/Scripts/python.exe -m pip_audit -r docs/security-review-271026/dependency-audit/dns-record-planning/resolved-pins.txt --no-deps --disable-pip --progress-spinner off -f json -o docs/security-review-271026/dependency-audit/dns-record-planning/audit.json`.
