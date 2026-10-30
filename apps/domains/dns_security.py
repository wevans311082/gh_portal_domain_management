"""Small, scoped DNSSEC facts. Missing evidence never means unsigned DNS."""
from django.core.exceptions import ValidationError

from apps.cloudflare_integration.scope import assert_provider_scope
from .dns_changes import backend_scope
from .contact_bindings import provider_scope as registrar_scope
from .bulk_observation import observed_nameservers


def provider_dnssec(*, provider, name, zone_id, client, scope):
    if provider == "cloudflare":
        assert_provider_scope(scope, client)
        payload = client.get_dnssec(zone_id)
        assert_provider_scope(scope, client)
        result = payload.get("result") if type(payload) is dict and payload.get("success") is True else None
        if type(result) is not dict or result.get("status") != "disabled":
            raise ValidationError("Cloudflare DNSSEC needs a separate reviewed migration plan.")
        if any(key in result and (type(result[key]) is not bool or result[key])
               for key in ("dnssec_multi_signer", "dnssec_presigned", "dnssec_use_nsec3")):
            raise ValidationError("Cloudflare DNSSEC features require a separate reviewed plan.")
    elif provider == "cpanel":
        if backend_scope(provider, client) != scope:
            raise ValidationError("The WHM DNSSEC account changed.")
        result = client.get_dnssec_status(name)
        if backend_scope(provider, client) != scope:
            raise ValidationError("The WHM DNSSEC account changed.")
        if type(result) is not dict or result.get("domain") != name or result.get("status") != "disabled":
            raise ValidationError("WHM DNSSEC needs a separate reviewed migration plan.")
    else:
        raise ValidationError("This DNS provider requires a reviewed DNSSEC adapter.")
    return {"provider": provider, "name": name, "zone_id": zone_id, "scope": scope, "status": "disabled"}


def registrar_dnssec(*, source, client):
    if registrar_scope(client) != source["registrar_scope"]:
        raise ValidationError("The registrar DNSSEC account changed.")
    payload = client.get_order_details(source["registrar_id"])
    if registrar_scope(client) != source["registrar_scope"]:
        raise ValidationError("The registrar DNSSEC account changed.")
    hosts = sorted(observed_nameservers(payload, source))
    # No unpublished status/enum mapping. Only an explicitly empty collection
    # is usable here; missing/null/string/false/zero and any records need review.
    # Registrar state is not proof of DS absence at the public parent zone.
    values = [payload.get("dnssec")] if type(payload) is dict else [None]
    if type(payload) is dict:
        values.extend(value for key, value in payload.items() if type(key) is str and key.endswith(".dnssec"))
        nested = payload.get("order_details")
        if type(nested) is dict:
            values.extend(value for key, value in nested.items() if key == "dnssec" or type(key) is str and key.endswith(".dnssec"))
    if any(type(value) not in (dict, list) or value for value in values):
        raise ValidationError("Registrar DNSSEC details require a separate reviewed plan or verified response contract.")
    return {"nameservers": hosts, "ds_records": [], "scope": source["registrar_scope"], "parent_ds_verified": False}
