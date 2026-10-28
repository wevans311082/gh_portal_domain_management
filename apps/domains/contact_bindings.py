"""Create immutable, type-scoped contacts instead of deprecated contacts/modify.

A durable reservation precedes the provider mutation. A crash, timeout or malformed
response leaves a blocked reservation, rather than causing another contact POST.
"""

import hashlib
import json
import re

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.domains.models import DomainContact, RegistrarContactBinding
from apps.domains.registrar_policy import policy_for
from apps.domains.resellerclub_client import ResellerClubClient


def _fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def provider_scope(client=None):
    from apps.core.runtime_settings import get_runtime_setting

    endpoint = getattr(client, "base_url", None)
    reseller = getattr(client, "reseller_id", None)
    if not isinstance(endpoint, str) or not endpoint:
        endpoint = ResellerClubClient._clean_secret(get_runtime_setting("RESELLERCLUB_API_URL", "https://httpapi.com/api")) or "https://httpapi.com/api"
    if not isinstance(reseller, str):
        reseller = ResellerClubClient._clean_secret(get_runtime_setting("RESELLERCLUB_RESELLER_ID", ""))
    # Scope by environment and reseller, but never persist credentials or URLs.
    return _fingerprint({"endpoint": endpoint.strip().rstrip("/"), "reseller": reseller.strip()})


def _remote_id(result):
    if isinstance(result, dict):
        result = next((result[key] for key in ("contact_id", "id", "contactid", "entityid") if result.get(key)), None)
    value = str(result).strip() if type(result) in (str, int) else ""
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise ValueError("The registrar contact creation outcome requires reconciliation.")
    return value


def resolve_contact_binding(contact, customer_id, tld, *, client=None):
    policy = policy_for(tld)
    # Validate before any database or network work, including unsaved invalid forms.
    contact.as_resellerclub_payload(customer_id=customer_id, tld=tld)
    if not contact.pk:
        raise ValueError("Save the contact before creating its registrar binding.")
    scope = provider_scope(client)
    # durable=True prevents an enclosing business transaction from rolling back
    # the reservation after the remote contact has already been created.
    with transaction.atomic(durable=True):
        current = DomainContact.objects.select_for_update().get(pk=contact.pk)
        if current.user_id != contact.user_id:
            raise ValidationError("The contact owner changed. Review the order.")
        payload = current.as_resellerclub_payload(customer_id=customer_id, tld=tld)
        digest = _fingerprint(payload)
        binding, created = RegistrarContactBinding.objects.get_or_create(
            contact=current, provider_scope=scope, customer_id=payload["customer-id"], contact_type=policy.contact_type,
            payload_digest=digest,
        )
        if not created:
            if binding.status == "ready" and re.fullmatch(r"[1-9][0-9]{0,18}", binding.remote_id):
                return binding.remote_id
            raise ValueError("Registrar contact creation is pending or uncertain. Support must reconcile it before retrying.")

    try:
        identifier = _remote_id((client or ResellerClubClient()).create_contact(payload))
    except BaseException:
        # Even provider errors are conservative: without a proven no-create
        # response contract, an automatic replacement could duplicate a contact.
        RegistrarContactBinding.objects.filter(pk=binding.pk, status="creating").update(status="unknown")
        raise

    with transaction.atomic(durable=True):
        binding = RegistrarContactBinding.objects.select_for_update().get(pk=binding.pk)
        if binding.status != "creating":
            raise ValueError("The contact binding was reconciled during creation. Review the provider result.")
        binding.remote_id = identifier
        binding.status = "ready"
        binding.save(update_fields=["remote_id", "status", "updated_at"])
        current = DomainContact.objects.select_for_update().get(pk=contact.pk)
        try:
            matches = _fingerprint(current.as_resellerclub_payload(customer_id=customer_id, tld=tld)) == digest
        except ValidationError:
            matches = False
        if not matches or current.user_id != contact.user_id:
            # Keep the known remote contact for its original immutable payload,
            # but never submit a domain with contact details edited mid-request.
            changed = True
        else:
            changed = False
    if changed:
        raise ValidationError("The contact changed during registrar creation. Review and retry the order.")
    return identifier
