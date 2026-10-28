# Shared registrant validation workstreams

Updated 28 October 2026 following the requirement to group checks and assign TLD lists, rather than build an independent KYC process for every extension.

## Workstreams

| Workstream | Shared responsibility | Reviewed membership / exceptions |
|---|---|---|
| Contact accuracy and entity type | Actual individual or organisation, reachable email, address, country and telephone; provider field bounds. Individuals do not need a company or Companies House record. | All enabled profiles. Provider contact types and field bounds remain profile constraints. |
| Identity and organisation assurance | Reusable, dated evidence against the current contact digest, reviewer and policy; separate person and business checks. | Customer/contact scope, independent of TLD. No automatic identity approval from declarations or contact formatting. Reuse only where evidence scope, freshness and policy permit it. |
| Registry eligibility | Shared declaration adapters and explicit registry exceptions. | General: com, net, org, io. UK accuracy: uk, co.uk, org.uk, me.uk; me.uk requires an individual. US nexus: us. EU presence: eu prepared, ordering still gated. |
| Registrar verification | Provider evidence, association checks, bounded refresh/resend and expiry. | ICANN RAA profile: com, net, org. Registry-specific profile: io, uk, co.uk, org.uk, me.uk, us. Registry-specific means the RAA workflow is not applicable; it does not mean verified. |
| Publication and privacy | Explicit consent, permitted privacy, withdrawal and reconciliation. | Existing organisation publication consent: com, net, org. Privacy capability remains explicit in each profile; UK and US profiles prohibit the optional service. EU prepared profile also prohibits it. |
| Domain operations | Shared paid submission, transfer, renewal, contact change and recovery workflows; profile-specific limits and holds. | All enabled profiles; UK routing, US registrant-only nexus and EU prepared delegation constraints remain explicit exceptions. |

## Implementation and rollout

`apps/domains/registrar_policy.py` is the reviewed membership catalogue. `PROFILE_TLDS` maps each shared profile to an explicit list. Catalogue construction rejects duplicate assignments, so a new list cannot silently replace another profile. Profiles select eligibility and email workstreams and service-contact routing. Shared contact serialization dispatches through the selected eligibility workstream.

Prepared contracts are separate from enabled membership. Saving or testing EU declarations does not enable EU ordering. Unknown extensions still require review; sharing a suffix category, contact type or apparent geography is insufficient to enable one.

Complete and verify each workstream across its assigned lists before expanding membership. New TLD onboarding should normally add a reviewed mapping and exception tests, rather than duplicate identity forms, evidence storage, queues or paid submission logic. Add a new adapter only when a documented registry requirement cannot be represented by an existing one.

Acceptance checks: individuals and organisations remain supported; local checks never create external verification evidence; evidence is not reused across incompatible scopes; service contacts do not inherit registrant-only declarations; unknown/prepared extensions remain gated; existing operation and privacy restrictions survive regrouping. ResellerClub payload and response contracts must be reviewed before enabling additional membership. This refactor does not claim new registry approval or live provider verification.
