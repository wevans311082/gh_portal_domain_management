# UK registry requirements and operation policies

This increment continues roadmap item 3. It adds reviewed local checks for the supported UK namespace, without making Companies House registration a prerequisite for individuals or claiming that local checks prove identity or registry approval. Complete contact replacement/IRTP and automated provider redaction remain outstanding.

## Sources and policy boundaries

Sources checked on 28 October 2026:

- ResellerClub [UK rules](https://manage.resellerclub.com/kb/servlet/KBServlet/faq1225.html), [contact creation](https://manage.resellerclub.com/kb/answer/790) and [registration contract](https://manage.resellerclub.com/kb/answer/752).
- Nominet [current legacy data completeness checks](https://registrars.nominet.uk/uk-namespace/data-quality-policy/data-completeness-checks/), [registrant fields](https://registrars.nominet.uk/uk-namespace/registration-and-domain-management/field-definitions-and-registrant-types/) and [legacy registration rules](https://media.nominet.uk/wp-content/uploads/2015/08/Rules_June_2014.pdf).
- Nominet's [policy transition page](https://registrars.nominet.uk/registry/dot-uk/policies/) explicitly distinguishes the currently operating legacy platform from future policies that take effect at transition. Future rules are not treated as already active.

There are documentary inconsistencies. The provider's detailed UK page says service contacts are no longer required, while an older registration parameter paragraph still describes them. No fabricated UK address/service contact is introduced. Registry field guidance describes a 63-character label; the provider still documents a 64-character total domain limit. New UK requests above that provider limit require review; existing registrations are not invalidated by that admission check. The old provider blanket two-letter restriction is not presented as a current registry ban: short names remain subject to actual provider availability and final registry acceptance. No live/funded contract test was performed to resolve these edge cases.

## Local admission

`registry_requirements.py` applies reviewed requirements before paid registration and is also called from customer forms, staff ordering, cart invoice preflight and the API registration boundary. UK contact serialization independently enforces applicable registrant constraints before contact creation or reuse.

- Actual individual names, or organization holder names, require four characters with at least three letters. Names are never padded or replaced.
- Numeric-only street information is rejected for UK registry contacts. This checks completeness; it does not verify a real postal address. International numbered address formats with non-numeric information are not arbitrarily rewritten.
- `.me.uk` new registration requires an individual registrant. Exceptional historical/court/dispute cases require review. A company number or business-record check does not satisfy this personal namespace requirement.
- `.co.uk` and `.org.uk` remain open to individuals and organizations. Their intended use is explained without inventing company-number or charity-registration requirements. Sole traders registering as persons are told to use their actual individual name rather than a trading name.
- Registration uses a single ASCII label, bounded term, reviewed namespace and applicable naming restrictions, including reserved UK labels and unsupported UK IDNs. Generic IDN registration requires its separate language workflow; no language attribute is invented.
- Nameservers are distinct valid hostnames, limited to 2-10 for UK and 2-13 for the reviewed general policy. WHM-supplied arrays are validated before normalization and before constructing the registrar client for an update.

UK role routing remains `UkContact` with only the registrant ID; unused admin/technical/billing IDs are `-1`. Unsupported registrar privacy is not purchased. No additional mandatory UK legal-type field or company number is invented from Nominet's encouraged optional classification metadata.

New registration attempts retain the reviewed policy version and safe constraint metadata with `scope=local_requirements_only`. Paid preparation/HTTP checks still reread the complete subject and current contacts. An acknowledged historical purchase is retained rather than purchased again under new checks.

## Namespace parsing and unsupported operations

Structural namespace recognition is separate from the advertised sales catalogue. `.me.uk` now parses correctly even if absent from the currently advertised extension list. Restricted/unimplemented suffixes such as `.ltd.uk`, `.plc.uk`, `.gov.uk` and `.sch.uk` cannot fall back to the open `.uk` policy. Existing unsupported policies still require eligibility review; this does not silently enable additional extensions or prices.

Operation capabilities use the actual domain suffix, not only the editable local TLD column. UK lock/EPP requests return a helpful, private 409 before any provider call. Detail pages omit those controls, avoid presenting a saved lock flag as an actual UK registrar lock and suppress cached EPP codes in the response. The cached database value is not represented as erased or rotated. Unknown extensions acquire no inferred sensitive-operation capability.

UK and `.io` pages also distinguish country-code checks from this portal's ICANN RAA email workflow. They show no implied RAA failure, universal 15-day deadline or RAA resend button. Queued RAA actions are admitted only for the reviewed generic extensions. Read-only provider observations remain distinct from eligibility and identity evidence.

## Existing .me.uk renewal checks

Before renewal invoice admission, direct renewal, queued provider work or automatic invoice scheduling, the application requires a consistent owned registrant association for `.me.uk`. A known organization or changed retained contact snapshot requires review. Existing cart items are rechecked before invoice creation. Domain/list pages show the review, omit bulk-renew selection and label configured auto-renew as needing review. Customers can turn it off; they cannot turn it on again until review is resolved. Court/dispute/grandfathered exceptions cannot be self-declared to bypass the check; support must confirm them with the registrar.

These are local admission checks, not proof of live registry eligibility. Legacy records without captured provider/contact evidence remain subject to the documented legacy policy and review limits. General renewal concurrency, payment-readiness and authoritative lifecycle reconciliation remain the later renewal/progress workstreams; this increment does not certify those broader behaviours.

## Verification

The initial UK suite passed 35 tests. The later combined registry/pricing/cart run passed 107; the final registry/registration/policy/staff/cart run passed 138 tests. Combined registry/verification/action/scan checks passed 167 before the final reserved-label refinements. The broad suite passed 1,667 tests with only the unchanged excluded nginx assertion. The final renewal-readiness refinement then passed 145 focused tests. An unrelated real-process fixture race was fixed with atomic PID-record publication; the production cleanup code and its termination assertions were retained.

Fourteen final desktop/mobile browser states cover registration guidance, organization rejection, UK domain controls, unsupported-operation recovery, `.me.uk` renewal review and visible renewal readiness on the domain/list pages. Contrast, labels, overflow and support/back links pass; representative mobile screenshots were inspected. Django and whitespace checks pass. No schema migration is needed for this increment; the pre-existing DomainOrder status-choice drift remains excluded from the feature migrations.

No production migration, provider mutation, payment or external message was performed. SQLite/mocked-provider tests do not establish live PostgreSQL locking behaviour, provider acceptance of every address or actual identity/registry verification. Those limits remain explicit rather than being replaced with a local "verified" flag.
