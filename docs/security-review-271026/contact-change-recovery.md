# Contact-change recovery

Contact-change submission and observation run through durable requests. Provider acknowledgements, IRTP approval and local contact edits are separate facts; none alone establishes completed replacement. Customer UI and final automatic confirmation remain in progress.

An active staff superuser may attribute an explicit recovery through the management command. Stop/review the relevant worker first; recovery requires an expired lease and at least 15 minutes since the request's last update. Supply a provider case reference and a reason of 10-1000 characters. References allow ASCII letters, digits, dots, underscores, colons and hyphens. Do not put credentials or contact details in either field.

To associate an independently identified registrar action with an uncertain submitted request:

```powershell
.venv/Scripts/python.exe manage.py reconcile_contact_change --change-id 123 --owner-id 1 --action link-action --action-id 701 --provider-reference CASE-701 --reason "Provider support confirmed the exact action for this request."
```

The service checks the captured provider scope and performs only an exact action lookup. It verifies order/action identifiers, rereads ownership, source identity and request state under locks, then records awaiting observation. It never calls modify-contact, creates contacts, changes current local associations or declares success. An action not found in current/archived results, mismatched evidence or a concurrent local change stops recovery. Linking an action does not itself prove that its operation completed successfully.

To stop stale preparation that has no submission marker:

```powershell
.venv/Scripts/python.exe manage.py reconcile_contact_change --change-id 123 --owner-id 1 --action cancel-preparation --provider-reference CASE-702 --reason "Reviewed worker history and confirmed no domain submission marker exists."
```

This records cancellation and replaces the worker token so a surviving preparation cannot reach domain submission. It does not delete or reset immutable contact-creation reservations; an uncertain contact binding still requires its own existing recovery workflow. Once a submission marker exists, this cancellation action is prohibited. A provider timeout is never treated as proof that no change occurred.

Both actions audit the attributed owner, request ID, case reference and reason. Provider bodies and exception text are excluded. No provider credentials, customer/account override or skip-authorization option is accepted by the command. These commands perform no deployment or swarm operations.

## Confirm successful completion

Until the legacy provider action-status contract supports automatic interpretation, an owner may explicitly attest that the provider case confirms successful completion of the exact retained action:

```powershell
.venv/Scripts/python.exe manage.py reconcile_contact_change --change-id 123 --owner-id 1 --action confirm-provider-completed --provider-success-confirmed --provider-reference CASE-703 --reason "Provider confirmed successful completion of this exact contact-change action."
```

This requires no live worker and a submission at least 15 minutes old. The service independently checks the exact action/order IDs and a completion timestamp within the request window, applicable generic-domain IRTP SUCCESS with both authorizations approved, every actual live contact role and the original immutable provider bindings. Current contacts, ownership, source association and publication evidence must still match the proposed versions. The flag cannot override any failed check. The owner must review the action type/status with the provider; an unknown enum is not automatically success.

Only then are current source contact links and remote IDs changed atomically with the confirmed request and audit record. Original paid-attempt snapshots remain unchanged, and no identity/business verification evidence is copied. Subsequent .me.uk renewal checks use the latest matching confirmed contact digest. This action does not claim completion of public-data redaction: provider publication-state reconciliation remains separate outstanding work.

Verification uses isolated database records and mocked provider reads. It does not establish live provider acceptance or PostgreSQL lock behaviour. Final automatic completion and redaction reconciliation remain outstanding.
