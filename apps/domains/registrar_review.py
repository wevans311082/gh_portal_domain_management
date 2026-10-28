"""Shared hold for provider operations on unconfirmed historical associations."""

from django.core.exceptions import ValidationError
from django.shortcuts import render

from apps.domains.models import DomainOrder, DomainTransfer

REVIEW_MESSAGE = "This domain's earlier registrar association requires owner review before provider operations or renewal can continue."


def registrar_review_required(domain):
    transfer = DomainTransfer.objects.filter(domain_id=domain.pk).values(
        "user_id", "domain_name", "registrar_order_id", "registrar_contact_ids", "status",
        "submission_attempt__legacy_review_required",
    ).first()
    if transfer:
        if (transfer["submission_attempt__legacy_review_required"] or transfer["status"] != "completed"
                or transfer["user_id"] != domain.user_id or transfer["domain_name"] != domain.name
                or transfer["registrar_order_id"] != domain.registrar_id):
            return True
        snapshot = transfer["registrar_contact_ids"]
        if not isinstance(snapshot, dict):
            return True
        if snapshot.get("_scope"):
            from apps.domains.contact_bindings import provider_scope
            try:
                if snapshot["_scope"] != provider_scope():
                    return True
            except Exception:
                return True
    link = DomainOrder.objects.filter(domain_id=domain.pk).values(
        "user_id", "domain_name", "registrar_order_id", "registrar_contact_ids", "registration_attempt__legacy_review_required",
    ).first()
    if link is None:
        return False
    if (link["registration_attempt__legacy_review_required"] or link["user_id"] != domain.user_id
            or link["domain_name"] != domain.name or link["registrar_order_id"] != domain.registrar_id):
        return True
    snapshot = link["registrar_contact_ids"]
    if not isinstance(snapshot, dict):
        return True
    if snapshot.get("_scope"):
        from apps.domains.contact_bindings import provider_scope
        try:
            return snapshot["_scope"] != provider_scope()
        except Exception:
            return True  # Configuration/key failures cannot switch the target account.
    return False  # Older records without a captured scope need the separate legacy review policy.


def assert_registrar_review_complete(domain):
    if registrar_review_required(domain):
        raise ValidationError(REVIEW_MESSAGE)


def review_response(request, domain):
    if not registrar_review_required(domain):
        return None
    response = render(request, "domains/registrar_review_required.html", {"domain": domain, "review_message": REVIEW_MESSAGE}, status=409)
    response["Cache-Control"] = "no-store"
    return response
