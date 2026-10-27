# Security review — 27 October 2026

This folder contains a point-in-time security review of the Cyber Ask Domains codebase.

- [Full review](security-review.md) — scope, findings, evidence, fixes, and positive controls.
- [Remediation plan](remediation-plan.md) — ordered work plan and acceptance criteria.
- [Application roadmap](application-roadmap.md) — 20 product improvements and new features, plus application security fixes; excludes Swarm and deployment changes.
- [Implementation status](implementation-status.md) — progress and verification evidence for every roadmap item.

The review identified **1 critical, 5 high, 3 moderate, and 1 low** severity findings. The most urgent issue is a confirmed MFA bypass caused by the production authentication-backend configuration.

This was a source-assisted application review, not an infrastructure penetration test. No production systems, accounts, or third-party services were accessed. The repository was reviewed in its current working-tree state, including the existing uncommitted admin-theme changes.

