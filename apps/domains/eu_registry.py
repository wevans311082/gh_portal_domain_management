"""Explicit EURid eligibility declarations; never infer citizenship from address."""

from django.core.exceptions import ValidationError

EU_COUNTRIES = frozenset("AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split())
ELIGIBLE_COUNTRIES = EU_COUNTRIES | {"IS", "LI", "NO"}
BASIS_CHOICES = [("", "Not requested"), ("resident", "Individual resident in an eligible country"),
    ("citizen", "Individual citizen of an eligible country"), ("established", "Organization established in an eligible country")]


def validate_eu_declaration(contact):
    errors = {}
    basis = contact.eu_eligibility_basis
    citizenship = contact.eu_citizenship_country
    if basis not in {"resident", "citizen", "established"}:
        errors["eu_eligibility_basis"] = "Declare the actual basis for EU registry eligibility."
    if contact.eu_eligibility_certified is not True:
        errors["eu_eligibility_certified"] = "Confirm the registrant's EU eligibility declaration."
    if basis in {"resident", "citizen"} and contact.registrant_type != "individual":
        errors["eu_eligibility_basis"] = "Residence and citizenship declarations require an individual registrant."
    if basis == "established" and contact.registrant_type != "organisation":
        errors["eu_eligibility_basis"] = "Establishment declarations require an organization registrant."
    if basis in {"resident", "established"} and contact.country not in ELIGIBLE_COUNTRIES:
        errors["country"] = "This address country requires EU registry eligibility/territory review for the chosen basis. Do not substitute a different address."
    if basis == "citizen" and citizenship not in ELIGIBLE_COUNTRIES:
        errors["eu_citizenship_country"] = "Declare citizenship of an EU member state, Iceland, Liechtenstein or Norway."
    if basis != "citizen" and citizenship:
        errors["eu_citizenship_country"] = "Citizenship is not needed for the chosen basis. Clear this field."
    if errors:
        raise ValidationError(errors)


def eu_contact_attributes(contact):
    validate_eu_declaration(contact)
    if contact.eu_eligibility_basis == "citizen" and contact.country not in ELIGIBLE_COUNTRIES:
        if contact.eu_citizenship_country not in EU_COUNTRIES:
            # EURid accepts these citizens, but the legacy provider attribute
            # documentation lists only EU27. Do not pretend that list is settled.
            raise ValidationError({"eu_citizenship_country": "EURid accepts this citizenship; the registrar's country attribute requires contract review before automated submission. Contact support."})
        return {"attr-name1": "countryOfCitizenship", "attr-value1": contact.eu_citizenship_country}
    return {}


def validate_eu_delegation_change(domain, requested, *, client):
    """Verify the full live delegation before using the simple NS editor.

    The ordinary portal has four fields and no child-host/glue workflow. Larger
    current delegations and any child-host involvement therefore require review.
    No current child may be hidden beyond the locally displayed four servers.
    """
    from types import SimpleNamespace
    from django.db import transaction
    from apps.accounts.models import User
    from apps.domains.contact_bindings import provider_scope
    from apps.domains.contact_change_confirmation import identifier
    from apps.domains.contact_changes import locked_source, _live_association
    from apps.domains.models import Domain
    from apps.domains.registry_requirements import validate_nameservers

    validate_nameservers(domain.name, requested)
    with transaction.atomic():
        User.objects.select_for_update().get(pk=domain.user_id)
        current = Domain.objects.select_for_update().get(pk=domain.pk)
        if current.user_id != domain.user_id or current.name != domain.name or current.registrar_id != domain.registrar_id:
            raise ValidationError("The registrar association changed. Reload the domain before updating nameservers.")
        source, attempt, kind = locked_source(current)
        if provider_scope(client) != attempt.provider_scope:
            raise ValidationError("The registrar environment changed. Review the association before updating nameservers.")
        payload = client.get_registration_reconciliation_details(current.registrar_id)
        target = SimpleNamespace(domain_name=current.name, registrar_order_id=current.registrar_id,
            customer_id=attempt.customer_id, subject={"previous_remote_ids": source.registrar_contact_ids})
        _live_association(payload, target)
        raw_count = identifier(payload.get("noOfNameServers"))
        if not raw_count or not 1 <= int(raw_count) <= 9:
            raise ValidationError("The live EU delegation needs review before nameserver changes.")
        count = int(raw_count)
        hosts = [payload.get(f"ns{index}") for index in range(1, count + 1)]
        validate_nameservers(current.name, hosts)
        if any(payload.get(f"ns{index}") for index in range(count + 1, 14)):
            raise ValidationError("The registrar delegation count is inconsistent. Contact support.")
        normalized = [host.strip().lower().rstrip(".") for host in hosts]
        proposed = [host.strip().lower().rstrip(".") for host in requested]
        child = lambda host: host == current.name or host.endswith("." + current.name)
        if any(child(host) for host in normalized + proposed):
            raise ValidationError("EU child nameservers require the dedicated child-server/glue workflow. Contact support; no delegation change was submitted.")
        if len(normalized) > 4:
            raise ValidationError("This EU domain has more than four live nameservers. Use a reviewed full-delegation workflow so hidden servers are not removed.")
