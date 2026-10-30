"""Read-only, per-item admission preview for bulk domain operations.

Snapshots are private service data, not browser tokens or provider approval.
Execution must recheck the snapshot and use durable submission fences.
"""
from dataclasses import dataclass, field
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from apps.accounts.models import User
from apps.admin_tools.secrets import IntegrationSecretError
from .models import Domain, DomainContact, ContactVerification
from .contact_bindings import _fingerprint
from .contact_changes import locked_source
from .registrar_policy import ALL_ROLES, policy_for, tld_for_domain, contact_payload_tld
from .registrar_review import assert_registrar_review_complete
from .registry_requirements import policy_snapshot, validate_nameservers, validate_renewal_registrant

MAX_ITEMS = 100
ACTIONS = ("lock", "auto_renew", "nameservers", "contacts")


@dataclass(frozen=True)
class BulkPreviewItem:
    domain_id: int
    name: str
    status: str
    message: str
    snapshot: dict = field(default_factory=dict, repr=False)


def normalize_request(action, domain_ids, target):
    if type(action) is not str or action not in ACTIONS:
        raise ValidationError("Choose lock, auto-renew, nameservers or contact changes.")
    if (type(domain_ids) is not list or not 1 <= len(domain_ids) <= MAX_ITEMS
            or any(type(pk) is not int or pk <= 0 or pk > 9223372036854775807 for pk in domain_ids)
            or len(set(domain_ids)) != len(domain_ids)):
        raise ValidationError(f"Choose between 1 and {MAX_ITEMS} distinct domains.")
    if type(target) is not dict:
        raise ValidationError("Choose valid target settings.")
    if action in ("lock", "auto_renew"):
        if set(target) != {"enabled"} or type(target["enabled"]) is not bool:
            raise ValidationError("Choose explicitly whether to enable or disable this setting.")
        normalized = {"enabled": target["enabled"]}
    elif action == "nameservers":
        if set(target) != {"nameservers"}:
            raise ValidationError("Supply only the target nameservers.")
        values = target["nameservers"]
        if (type(values) is not list or not 1 <= len(values) <= 13
                or any(type(value) is not str or len(value) > 253 for value in values)):
            raise ValidationError("Supply between 1 and 13 valid nameserver hostnames.")
        values = [value.strip().lower().rstrip(".") for value in values]
        from django.core.validators import validate_domain_name
        for value in values:
            validate_domain_name(value)
        if len(set(values)) != len(values):
            raise ValidationError("Choose distinct nameserver hostnames.")
        normalized = {"nameservers": values}
    else:
        if set(target) != {"contacts", "publication_choice"}:
            raise ValidationError("Supply the proposed contacts and publication choice.")
        roles = target["contacts"]
        if (type(roles) is not dict or not roles or set(roles) - set(ALL_ROLES)
                or any(type(pk) is not int or pk <= 0 or pk > 9223372036854775807 for pk in roles.values())):
            raise ValidationError("Choose owned contacts for the applicable roles.")
        if type(target["publication_choice"]) is not str or target["publication_choice"] not in ("redact", "publish"):
            raise ValidationError("Choose whether to redact or publish the organization.")
        normalized = {"contacts": dict(roles), "publication_choice": target["publication_choice"]}
    return action, list(domain_ids), normalized


def _snapshot(domain, action, target, *, bulk_item_id=None):
    from .bulk_reservations import assert_no_bulk_operation
    assert_no_bulk_operation(domain, item_id=bulk_item_id)
    # Disabling this local preference must remain possible even during registrar review.
    if action == "auto_renew" and not target["enabled"]:
        return {"domain_id": domain.pk, "owner_id": domain.user_id, "name": domain.name,
                "status": domain.status, "auto_renew": domain.auto_renew, "local_only": True}
    if domain.status != Domain.STATUS_ACTIVE:
        raise ValidationError("This operation requires an active domain.")
    policy = policy_for(tld_for_domain(domain.name))
    if action == "lock" and not policy.lock_supported:
        raise ValidationError("This registry does not support registrar transfer locks.")
    assert_registrar_review_complete(domain, bulk_item_id=bulk_item_id)
    source, attempt, kind = locked_source(domain)
    snapshot = {"domain_id": domain.pk, "owner_id": domain.user_id, "name": domain.name,
        "status": domain.status, "registrar_id": domain.registrar_id,
        "provider_scope": attempt.provider_scope, "customer_id": attempt.customer_id,
        "source_kind": kind, "source_id": source.pk, "policy": policy_snapshot(domain.name),
        "remote_contacts": dict(source.registrar_contact_ids)}
    if action == "auto_renew":
        validate_renewal_registrant(domain)
        snapshot["auto_renew"] = domain.auto_renew
    elif action == "lock":
        snapshot["is_locked"] = domain.is_locked
    elif action == "nameservers":
        validate_nameservers(domain.name, target["nameservers"])
        snapshot["previous_nameservers"] = domain.cached_nameservers
    else:
        if set(target["contacts"]) != set(policy.roles):
            raise ValidationError("The proposed contact roles do not match this registry's requirements.")
        selected = {contact.pk: contact for contact in DomainContact.objects.select_for_update().filter(
            user_id=domain.user_id, pk__in=target["contacts"].values()).order_by("pk")}
        digests = {}
        for role in policy.roles:
            contact = selected.get(target["contacts"][role])
            if contact is None:
                raise ValidationError("Every proposed contact must belong to this customer.")
            contact.as_resellerclub_payload(attempt.customer_id, contact_payload_tld(tld_for_domain(domain.name), role))
            digests[role] = ContactVerification.digest_for(contact)
        from .publication_consent import validate_choice
        validate_choice(target["publication_choice"], selected[target["contacts"]["registration_contact"]], domain.name)
        snapshot["contact_digests"] = digests
        snapshot["previous_roles"] = {role: getattr(source, role + "_id") for role in policy.roles}
    return snapshot


@transaction.atomic
def preview_bulk(*, customer, action, domain_ids, target):
    action, domain_ids, target = normalize_request(action, domain_ids, target)
    owner = User.objects.select_for_update().get(pk=customer.pk)
    if not owner.is_active or owner.is_staff or owner.is_superuser:
        raise PermissionDenied
    owned = {domain.pk: domain for domain in Domain.objects.select_for_update().filter(
        user=owner, pk__in=domain_ids).order_by("pk")}
    items = []
    for pk in domain_ids:
        domain = owned.get(pk)
        if domain is None:
            items.append(BulkPreviewItem(pk, "Unavailable domain", "unavailable", "This domain is not available to this account."))
            continue
        try:
            snapshot = _snapshot(domain, action, target)
        except ValidationError as exc:
            items.append(BulkPreviewItem(pk, domain.name, "held", " ".join(exc.messages)))
            continue
        except IntegrationSecretError:
            items.append(BulkPreviewItem(pk, domain.name, "held", "Registrar configuration requires owner review."))
            continue
        snapshot["action"] = action
        snapshot["target"] = target
        snapshot["digest"] = _fingerprint(snapshot)
        # A cached registrar lock is not proof of the provider's current state.
        unchanged = action == "auto_renew" and domain.auto_renew == target["enabled"]
        items.append(BulkPreviewItem(pk, domain.name, "unchanged" if unchanged else "ready",
            "The requested setting is already recorded locally." if unchanged else "Ready for confirmation and execution checks.", snapshot))
    return {"action": action, "target": target, "items": items,
        "cost_notice": "No charge is calculated or authorized by this preview. Contact-holder changes requiring a separate registry transaction must use that reviewed workflow."}
