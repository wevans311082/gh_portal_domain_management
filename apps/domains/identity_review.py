"""Version-bound local review metadata, independent of registrar verification."""

from datetime import timedelta
import re
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.models import ContactVerification, DomainContact


def record_local_review(*, actor, contact_id, key, subject_digest, status, policy_reference,
                        evidence_reference, valid_days, check_kind, policy_reviewed=False):
    if (check_kind not in {"identity", "business"}
            or status not in {"pending", "verified", "rejected", "unknown"} or type(valid_days) is not int
            or not 1 <= valid_days <= 365 or policy_reviewed is not True
            or not isinstance(subject_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", subject_digest)):
        raise ValidationError("Choose a local review outcome, 1-365 day validity and confirm the referenced policy review.")
    for reference in (policy_reference, evidence_reference):
        if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", reference):
            raise ValidationError("Use policy and evidence case references, without personal details or credentials.")
    try:
        key = uuid.UUID(str(key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Reload the review form before recording evidence.") from None
    identity = DomainContact.objects.values("user_id").get(pk=contact_id)
    with transaction.atomic():
        customer = User.objects.select_for_update().get(pk=identity["user_id"])
        contact = DomainContact.objects.select_for_update().get(pk=contact_id)
        reviewer = User.objects.select_for_update().get(pk=actor.pk)
        if not reviewer.is_active or not reviewer.is_staff or not reviewer.is_superuser:
            raise PermissionDenied
        if (customer.is_staff or customer.is_superuser or contact.user_id != customer.pk
                or ContactVerification.digest_for(contact) != subject_digest):
            raise ValidationError("The contact or customer changed. Review the current version before recording an outcome.")
        # This is format/completeness only; never a substitute for policy review.
        contact.as_resellerclub_payload("1", "com")
        if check_kind == "business" and contact.registrant_type != "organisation":
            raise ValidationError("Business reviews apply to organisations and sole traders. Individuals do not need a business review.")
        previous = ContactVerification.objects.filter(review_key=key).first()
        if previous:
            if (previous.contact_id != contact.pk or previous.reviewer_id != reviewer.pk or previous.check_kind != check_kind
                    or previous.subject_digest != subject_digest or previous.status != status
                    or previous.policy_reference != policy_reference or previous.reference != evidence_reference
                    or previous.expires_at is None or (previous.expires_at - previous.checked_at).days != valid_days):
                raise ValidationError("The review submission key belongs to another outcome. Reload the form.")
            return previous
        now = timezone.now()
        evidence = ContactVerification.objects.create(contact=contact, check_kind=check_kind, status=status,
            subject_digest=subject_digest, source="local-policy-review", reference=evidence_reference,
            policy_reference=policy_reference, reviewer=reviewer, review_key=key,
            checked_at=now, expires_at=now + timedelta(days=valid_days))
        AuditLog.objects.create(user=reviewer, action=f"domain.contact.{check_kind}_reviewed", model_name="DomainContact",
            object_id=str(contact.pk), data={"verification_id": evidence.pk, "status": status})
        return evidence


def record_identity_review(**kwargs):
    """Compatibility entry point with a fixed identity scope."""
    return record_local_review(check_kind="identity", **kwargs)
