"""Reconcile provider RAA evidence without promoting local validation flags.

Contract: https://manage.resellerclub.com/kb/answer/770 (checked 2026-10-28).
"""

from datetime import datetime, timedelta, timezone as datetime_timezone

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.domains.models import ContactVerification, DomainContact, DomainOrder


def registrar_email_summary(domain):
    """Return safe, domain-scoped display data without calling a provider."""
    result = {"status": "unknown", "label": "Not confirmed", "checked_at": None, "action_due_at": None,
              "can_refresh": False, "can_resend": False, "action_label": "", "needs_review": False}
    order = DomainOrder.objects.filter(
        domain_id=domain.pk, user_id=domain.user_id, domain_name=domain.name,
    ).first()
    if order is None:
        return result
    attempt = getattr(order, "registration_attempt", None)
    if attempt and attempt.legacy_review_required:
        result.update(label="Historical owner review required", needs_review=True, review_message="The earlier registrar association needs owner review. Contact support before requesting another registrar check.")
        return result
    from apps.domains.registrar_review import registrar_review_required
    if registrar_review_required(domain):
        result.update(label="Registrar association review required", needs_review=True, review_message="The saved domain association does not match the current registrar account or record. Contact support before requesting another registrar check.")
        return result
    result["can_refresh"] = bool(_identifier(order.registrar_order_id) and _identifier(order.remote_registrant_id))
    review = order.verification_actions.filter(status="unknown").first()
    result["needs_review"] = bool(review)
    action = review or order.verification_actions.first()
    if action:
        label = "Automatic status check" if action.is_automatic else action.get_kind_display()
        result["action_label"] = f"{label}: {action.get_status_display()}"
    evidence = ContactVerification.objects.filter(
        domain_order=order, contact_id=order.registration_contact_id,
        check_kind="registrar_email", source="resellerclub",
    ).first()
    if evidence is None:
        return result
    result["checked_at"] = evidence.checked_at
    if not evidence.is_current:
        result["label"] = "Needs recheck"
        return result
    result.update(status=evidence.status, label=evidence.get_status_display())
    if evidence.status in {"pending", "suspended"}:
        result["action_due_at"] = evidence.action_due_at
        result["can_resend"] = not review and result["can_refresh"] and order.tld in {"com", "net", "org"}
    return result


def _identifier(value):
    # Reject booleans, containers and accidental float coercion.
    if type(value) not in {str, int}:
        return ""
    value = str(value).strip()
    if not value.isascii() or not value.isdigit() or len(value) > 19:
        return ""
    return value if int(value) > 0 else ""


def _start_time(value, checked_at):
    value = _identifier(value)
    if not value:
        return None
    try:
        started = datetime.fromtimestamp(int(value), tz=datetime_timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return started if started <= checked_at else None


def reconcile_registrant_verification(order_id, customer_id, *, client):
    """Read provider state, bind it to the current subject and retain safe evidence.

    Network I/O occurs outside the transaction. Rechecking the subject and order
    under row locks prevents an intervening edit from acquiring stale evidence.
    Raw provider payloads, auth codes and identity documents are not persisted.
    """
    order = DomainOrder.objects.select_related("registration_contact").get(pk=order_id)
    attempt = getattr(order, "registration_attempt", None)
    if attempt and attempt.legacy_review_required:
        raise ValidationError("Historical owner review is required before registrar verification.")
    contact = order.registration_contact
    from apps.domains.registrar_policy import tld_for_domain
    if tld_for_domain(order.domain_name) != order.tld:
        raise ValidationError("The domain extension does not match the stored order.")
    from apps.domains.contact_bindings import provider_scope
    if order.registrar_contact_ids.get("_scope") and order.registrar_contact_ids["_scope"] != provider_scope(client):
        raise ValidationError("The registrar environment changed. Reconcile the order before verification.")
    remote_order = _identifier(order.registrar_order_id)
    remote_contact = _identifier(order.remote_registrant_id)
    expected_customer = _identifier(customer_id)
    if (attempt and attempt.status == "accepted" and attempt.remote_order_id == remote_order
            and attempt.provider_scope == order.registrar_contact_ids.get("_scope") and _identifier(attempt.customer_id)):
        expected_customer = _identifier(attempt.customer_id)
    if not all((remote_order, remote_contact, expected_customer)) or contact.user_id != order.user_id:
        raise ValidationError("A linked registrar order and owned registrant contact are required.")
    snapshot = ContactVerification.digest_for(contact)
    checked_at = timezone.now()
    payload = client.get_registrant_verification_details(remote_order)
    if not isinstance(payload, dict):
        raise ValidationError("The registrar returned an unsupported verification response.")
    remote_details = payload.get("registrantcontact")
    if (
        _identifier(payload.get("orderid", payload.get("entityid"))) != remote_order
        or str(payload.get("domainname", "")).strip().lower() != order.domain_name.lower()
        or _identifier(payload.get("customerid")) != expected_customer
        or _identifier(payload.get("registrantcontactid")) != remote_contact
        or not isinstance(remote_details, dict)
        or str(remote_details.get("email", "")).strip().casefold() != contact.email.strip().casefold()
    ):
        raise ValidationError("Registrar verification could not be matched to the current domain and contact.")

    status = {"Verified": "verified", "Pending": "pending", "Suspended": "suspended"}.get(
        payload.get("raaVerificationStatus") if isinstance(payload.get("raaVerificationStatus"), str) else "",
        "unknown",
    )
    started = _start_time(payload.get("raaVerificationStartTime"), checked_at) if status in {"pending", "suspended"} else None
    # The 15-day RAA rule is not a universal ccTLD or identity-check deadline.
    due = started + timedelta(days=15) if started and order.tld in {"com", "net", "org"} else None
    with transaction.atomic():
        current_order = DomainOrder.objects.select_for_update().get(pk=order.pk)
        current = DomainContact.objects.select_for_update().get(pk=contact.pk)
        if (
            current_order.registration_contact_id != contact.pk
            or current_order.registrar_contact_ids != order.registrar_contact_ids
            or current_order.remote_registrant_id != remote_contact
            or current_order.registrar_order_id != remote_order
            or current_order.domain_name != order.domain_name
            or current_order.tld != order.tld
            or current_order.user_id != order.user_id
            or ContactVerification.digest_for(current) != snapshot
        ):
            raise ValidationError("The contact or order changed during verification. Check again.")
        return ContactVerification.objects.create(
            contact=current, check_kind="registrar_email", status=status,
            subject_digest=snapshot, source="resellerclub", reference=remote_order,
            tld=order.tld, checked_at=checked_at, expires_at=checked_at + timedelta(minutes=15),
            provider_started_at=started, action_due_at=due,
            domain_order=current_order, domain_name=order.domain_name, remote_contact_id=remote_contact,
            provider_scope=order.registrar_contact_ids.get("_scope", ""),
        )
