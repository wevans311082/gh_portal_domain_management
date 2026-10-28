"""Read-only verification of a historical provider order before local adoption."""

from datetime import datetime, timezone as datetime_timezone
import hashlib

from django.core.exceptions import ValidationError
from django.core.validators import validate_domain_name
from django.db import transaction
from django.utils import timezone

from apps.domains.registration_submission import _identifier


def identifier_digest(value):
    return hashlib.sha256(str(value or "").encode()).hexdigest()


def historical_subject(order):
    """Metadata only; no unconfirmed diagnostics or contact payloads are copied."""
    from apps.domains.registrar_policy import ALL_ROLES
    fields = ("user_id", "invoice_id", "domain_name", "tld", "registration_years", "privacy_enabled", "auto_renew", "dns_provider", "quoted_price", "total_price")
    roles = ALL_ROLES[:1] if order.tld in {"uk", "co.uk", "org.uk", "me.uk"} else ALL_ROLES
    return {"order": {field: str(getattr(order, field)) if field in {"quoted_price", "total_price"} else getattr(order, field) for field in fields},
            "roles": {role: getattr(order, role + "_id") for role in roles}, "contacts": {}, "nameservers": [],
            "legacy_original_status": order.status, "legacy_identifier_digest": identifier_digest(order.registrar_order_id)}


def verified_existing_order(payload, *, identifier, domain_name, customer_id):
    """Return only allowlisted facts. Never infer contact identity verification."""
    if (
        not isinstance(payload, dict)
        or _identifier(payload.get("orderid", payload.get("entityid"))) != identifier
        or not isinstance(payload.get("domainname"), str)
        or payload["domainname"].strip().lower().rstrip(".") != domain_name.strip().lower().rstrip(".")
        or _identifier(payload.get("customerid")) != customer_id
    ):
        raise ValidationError("The historical registrar order could not be matched.")
    raw_status = payload.get("currentstatus")
    status = {"Active": "active", "InActive": "pending", "Suspended": "suspended"}.get(raw_status) if isinstance(raw_status, str) else None
    if not status:
        raise ValidationError("Review the registrar lifecycle state before adopting this order.")
    role_ids = {}
    for key, field in (("reg", "registrantcontactid"), ("admin", "admincontactid"), ("tech", "techcontactid"), ("billing", "billingcontactid")):
        value = _identifier(payload.get(field))
        if key == "reg" and not value:
            raise ValidationError("The historical registrant contact identifier is missing.")
        role_ids[key] = value or "-1"
    nameservers = []
    from apps.domains.registrar_policy import POLICIES, tld_for_domain
    policy = POLICIES.get(tld_for_domain(domain_name))
    count = payload.get("noOfNameServers")
    if status == "pending" and count is None and not payload.get("ns1") and not payload.get("ns2"):
        count = 0
    if type(count) not in (str, int) or len(str(count)) > 2 or not str(count).isascii() or not str(count).isdigit():
        raise ValidationError("The registrar nameservers require review.")
    count = int(count)
    minimum = policy.min_nameservers if policy else 2
    maximum = policy.max_nameservers if policy else 13
    if not (minimum <= count <= maximum or status == "pending" and count == 0):
        raise ValidationError("The registrar nameservers require review.")
    if count == 0 and any(payload.get(f"ns{index}") for index in range(1, 14)):
        raise ValidationError("The registrar nameservers require review.")
    for index in range(1, int(count) + 1):
        value = payload.get(f"ns{index}")
        if not isinstance(value, str):
            raise ValidationError("The registrar nameservers require review.")
        value = value.strip().lower().rstrip(".")
        validate_domain_name(value)
        if len(value) > 253 or value in nameservers:
            raise ValidationError("The registrar nameservers require review.")
        nameservers.append(value)
    if policy:
        for role in policy.roles:
            key = "reg" if role == "registration_contact" else role.removesuffix("_contact")
            if role_ids[key] == "-1":
                raise ValidationError("The registrar role identifiers require review.")
    if tld_for_domain(domain_name) in {"uk", "co.uk", "org.uk", "me.uk"} and len(nameservers) > 10:
        raise ValidationError("The UK registrar nameservers require review.")
    dates = []
    for field in ("creationtime", "endtime"):
        raw = payload.get(field)
        if status == "pending" and (raw is None or type(raw) is str and raw.strip() in {"", "0"} or type(raw) is int and raw == 0):
            dates.append(None)
            continue
        stamp = _identifier(raw)
        if not stamp:
            raise ValidationError("The registrar registration dates require review.")
        try:
            value = datetime.fromtimestamp(int(stamp), tz=datetime_timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            raise ValidationError("The registrar registration dates require review.") from None
        if not 1985 <= value.year <= 2200:
            raise ValidationError("The registrar registration dates require review.")
        dates.append(value)
    if (all(dates) and dates[1] <= dates[0]) or (dates[0] is not None and dates[0] > timezone.now().date()):
        raise ValidationError("The registrar registration dates require review.")
    return {"status": status, "registered_at": dates[0], "expires_at": dates[1], "nameservers": nameservers, "role_ids": role_ids, "checked_at": timezone.now()}


def review_historical_registration(attempt, owner, *, action, identifier, customer_id, reference, reason, client=None):
    """Audited owner review. Provider reads cannot become a paid mutation."""
    import uuid
    from apps.accounts.models import User
    from apps.audit.models import AuditLog
    from apps.domains.contact_bindings import provider_scope
    from apps.domains.models import DomainOrder, DomainRegistrationAttempt
    from apps.domains.registration_submission import _same_order
    from apps.domains.registrar_policy import tld_for_domain

    if not User.objects.filter(pk=owner.pk, is_active=True, is_staff=True, is_superuser=True).exists():
        raise ValidationError("An active owner is required for historical review.")
    if not attempt.legacy_review_required or attempt.status != "unknown":
        raise ValidationError("Choose a historical order awaiting its original review.")
    facts = None
    scope = ""
    if action == "adopt-legacy":
        if not identifier or not customer_id or client is None:
            raise ValidationError("Supply valid provider order and customer IDs for historical adoption.")
        scope = provider_scope(client)
        try:
            payload = client.get_registration_reconciliation_details(identifier)
            facts = verified_existing_order(payload, identifier=identifier, domain_name=attempt.subject["order"]["domain_name"], customer_id=customer_id)
        except Exception:
            raise ValidationError("Historical provider identity, status, dates or nameservers could not be confirmed. No linkage was changed.") from None
    elif action != "confirm-no-order" or identifier or customer_id:
        raise ValidationError("Choose historical adoption or an explicit no-existing-or-pending-order confirmation.")
    with transaction.atomic():
        order = DomainOrder.objects.select_for_update().get(pk=attempt.order_id)
        current = DomainRegistrationAttempt.objects.select_for_update().get(pk=attempt.pk)
        actor = User.objects.select_for_update().filter(pk=owner.pk, is_active=True, is_staff=True, is_superuser=True).first()
        if actor is None:
            raise ValidationError("Owner access changed during historical review.")
        if current.token != attempt.token or current.updated_at != attempt.updated_at or current.status != "unknown" or not current.legacy_review_required:
            raise ValidationError("The historical attempt changed during review.")
        if not _same_order(current, order) or current.subject.get("legacy_identifier_digest") != identifier_digest(order.registrar_order_id):
            raise ValidationError("The historical order changed. Review its current identity before resolving it.")
        if facts:
            canonical = order.domain_name.strip().lower().rstrip(".")
            validate_domain_name(canonical)
            if DomainOrder.objects.filter(domain_name__iexact=canonical).exclude(pk=order.pk).exists():
                raise ValidationError("Another platform order uses this domain. Resolve the duplicate before adopting it.")
            if order.domain_id:
                from apps.domains.models import Domain
                linked = Domain.objects.select_for_update().get(pk=order.domain_id)
                if linked.user_id != order.user_id or linked.name != canonical or linked.registrar_id != identifier:
                    raise ValidationError("The historical local domain link conflicts with the confirmed provider order. Resolve that link before adoption.")
            order.domain_name = canonical
            order.tld = tld_for_domain(canonical)
            current.subject = {**current.subject, "nameservers": facts["nameservers"],
                               "order": {**current.subject["order"], "domain_name": canonical, "tld": order.tld}}
            current.role_ids = {"_scope": scope, **facts["role_ids"]}
            current.provider_scope = scope
            current.customer_id = customer_id
            current.remote_order_id = identifier
            current.registry_status = facts["status"]
            current.registry_checked_at = facts["checked_at"]
            current.registry_registered_at = facts["registered_at"]
            current.registry_expires_at = facts["expires_at"]
            current.status = "accepted"
            order.registrar_order_id = identifier
            order.registrar_contact_ids = current.role_ids
            order.save(update_fields=["domain_name", "tld", "registrar_order_id", "registrar_contact_ids", "updated_at"])
        else:
            # A known historical registrar ID is not absence evidence. An expired
            # or archived purchase needs lifecycle/payment review, not reuse of
            # an old paid order as a fresh registration.
            if _identifier(order.registrar_order_id) or order.domain_id:
                raise ValidationError("A known registrar order or linked asset must be read and reviewed; it cannot be released as an unsubmitted purchase.")
            current.status = "aborted"
            current.token = uuid.uuid4()
            current.role_ids = {}
            order.registrar_order_id = ""
            order.registrar_contact_ids = {}
            order.save(update_fields=["registrar_order_id", "registrar_contact_ids", "updated_at"])
        current.legacy_review_required = False
        current.completed_at = timezone.now()
        current.save()
        AuditLog.objects.create(user=actor, action="domain.registration.historical_reviewed", model_name="DomainOrder", object_id=str(order.pk), data={
            "registration_attempt_id": current.pk, "resolution": action, "provider_reference": reference, "reason": reason,
        })
    return current
