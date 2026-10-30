"""Strict read-only lock evidence; never infer unlock from an omitted field."""
from django.core.exceptions import ValidationError
from .contact_change_confirmation import identifier
from .registrar_policy import policy_for, tld_for_domain


def observed_association(payload, snapshot):
    error = "The registrar order evidence could not be matched."
    if type(payload) is not dict or len(payload) > 200 or str(payload.get("status", "")).upper() == "ERROR":
        raise ValidationError(error)
    ids = [identifier(payload[key]) for key in ("orderid", "entityid") if key in payload]
    if (not ids or any(value != snapshot["registrar_id"] for value in ids)
            or identifier(payload.get("customerid")) != snapshot["customer_id"]
            or type(payload.get("domainname")) is not str
            or payload["domainname"].lower().rstrip(".") != snapshot["name"].lower().rstrip(".")
            or payload.get("currentstatus") != "Active"):
        raise ValidationError(error)
    roles = policy_for(tld_for_domain(snapshot["name"])).roles
    remote = {"registration_contact": ("reg", "registrantcontactid"), "admin_contact": ("admin", "admincontactid"),
              "tech_contact": ("tech", "techcontactid"), "billing_contact": ("billing", "billingcontactid")}
    for selected in roles:
        role, field = remote[selected]
        expected = snapshot["remote_contacts"].get(role)
        if not identifier(expected) or identifier(payload.get(field)) != expected:
            raise ValidationError(error)


def observed_lock(payload, snapshot):
    observed_association(payload, snapshot)
    flags = payload.get("orderstatus")
    if type(flags) is str:
        flags = [flags]  # Only an explicit known singleton is accepted as text.
    if (type(flags) is not list or len(flags) > 3 or any(type(value) is not str for value in flags)
            or len(set(flags)) != len(flags) or set(flags) - {"transferlock"}):
        raise ValidationError("The registrar registry-lock flags require review.")
    # An explicit empty collection is distinct from absent/null/blank/unknown evidence.
    return "transferlock" in flags


def observed_nameservers(payload, snapshot):
    """Require an explicit complete count and distinct hosts, not an ns1/ns2 prefix."""
    observed_association(payload, snapshot)
    return complete_nameservers(payload, snapshot["name"])


def complete_nameservers(payload, domain_name):
    """Parse a bounded, explicitly counted delegation without inferring authority."""
    import re
    from .registry_requirements import validate_nameservers
    if type(payload) is not dict or len(payload) > 200:
        raise ValidationError("The registrar nameserver details require review.")
    count = payload.get("noOfNameServers")
    if (type(count) not in (int, str) or len(str(count)) > 2
            or not str(count).isascii() or not str(count).isdigit() or not 1 <= int(count) <= 13):
        raise ValidationError("The registrar nameserver count requires review.")
    count = int(count)
    result = []
    for index in range(1, count + 1):
        value = payload.get(f"ns{index}")
        if type(value) is not str or len(value) > 253:
            raise ValidationError("The registrar nameserver set is incomplete.")
        result.append(value.strip().lower().rstrip("."))
    for key, value in payload.items():
        if type(key) is str and re.fullmatch(r"ns[0-9]+", key) and value not in (None, ""):
            if len(key) > 4:
                raise ValidationError("The registrar nameserver fields exceed supported bounds.")
            number = int(key[2:])
            if not 1 <= number <= count or key != f"ns{number}":
                raise ValidationError("The registrar nameserver fields contradict its declared count.")
    validate_nameservers(domain_name, result)
    return result
