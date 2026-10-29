"""Immutable agreement publication and customer acceptance; no inferred consent.

Paid submission consumes current evidence before durable admission and frozen
evidence afterwards. Invoice/quote conversion gates remain separate integration.
No agreement text is fetched, generated or pre-approved by this module.
"""

from dataclasses import dataclass
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator, validate_domain_name
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.domains.models import (
    ContactVerification, DomainContact, RegistrarAgreementVersion, RegistrarAgreementBinding,
    RegistrarAgreementCatalogue, DomainAgreementAcceptance, DomainAgreementAcceptanceDocument,
)
from apps.domains.registrar_policy import PROFILE_TLDS, policy_for, profile_key_for, tld_for_domain

OPERATIONS = ("registration", "transfer")
DOCUMENT_FIELDS = ("agreement_key", "version", "title", "body", "source_url", "profile_keys", "operations")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Use a fresh agreement submission key.") from None


def _document_data(document):
    return {field: getattr(document, field) for field in DOCUMENT_FIELDS}


def publication_preview_digest(**data):
    """Bind an owner preview to the exact canonical text and applicability."""
    return _digest(_publication_data(**data))


def agreement_document_is_intact(document):
    try:
        data = _document_data(document)
        if document.source_url:
            URLValidator(schemes=["https"])(document.source_url)
            if urlsplit(document.source_url).username is not None or urlsplit(document.source_url).password is not None:
                return False
        return document.document_digest == _digest(data) and document.published_at <= timezone.now()
    except (ValidationError, TypeError, ValueError):
        return False


def _catalogue_lock():
    RegistrarAgreementCatalogue.objects.get_or_create(pk=1)
    return RegistrarAgreementCatalogue.objects.select_for_update().get(pk=1)


def _publication_data(agreement_key, version, title, body, source_url, profile_keys, operations):
    if not isinstance(agreement_key, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", agreement_key):
        raise ValidationError("Use a lowercase agreement key of up to 80 letters, digits and hyphens.")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", version):
        raise ValidationError("Use a version reference of up to 80 letters, digits, dots, underscores and hyphens.")
    for text, maximum in ((title, 200), (body, 20000)):
        if (not isinstance(text, str) or not text.strip() or len(text) > maximum
                or any(ord(char) < 32 and char not in "\r\n\t" or ord(char) == 127 for char in text)):
            raise ValidationError("Supply a title of up to 200 characters and approved agreement text of up to 20,000 characters, without control characters.")
    if not isinstance(source_url, str) or len(source_url) > 500:
        raise ValidationError("Use an optional HTTPS source reference of up to 500 characters.")
    if source_url:
        URLValidator(schemes=["https"])(source_url)
        if urlsplit(source_url).username is not None or urlsplit(source_url).password is not None:
            raise ValidationError("Source references must not contain credentials.")
    for values, allowed in ((profile_keys, PROFILE_TLDS), (operations, OPERATIONS)):
        if (not isinstance(values, (list, tuple)) or not values or len(values) > len(allowed)
                or any(not isinstance(value, str) or value not in allowed for value in values)
                or len(set(values)) != len(values)):
            raise ValidationError("Select distinct reviewed profiles and registration/transfer operations.")
    return dict(agreement_key=agreement_key, version=version, title=title, body=body, source_url=source_url,
                profile_keys=sorted(profile_keys), operations=sorted(operations))


@transaction.atomic
def publish_agreement(*, actor, submission_key, agreement_key, version, title, body,
                      profile_keys, operations, source_url="", reviewed=False, expected_revision=None):
    owner = User.objects.select_for_update().get(pk=actor.pk)
    if not owner.is_active or not owner.is_staff or not owner.is_superuser:
        raise PermissionDenied
    if reviewed is not True:
        raise ValidationError("Confirm that the exact agreement text and selected scope have been approved for publication.")
    data = _publication_data(agreement_key, version, title, body, source_url, profile_keys, operations)
    key = _uuid(submission_key)
    catalogue = _catalogue_lock()
    previous = RegistrarAgreementVersion.objects.filter(submission_key=key).first()
    if previous:
        if _document_data(previous) != data or previous.publisher_id != owner.pk:
            raise ValidationError("This publication key belongs to another document or scope.")
        return previous
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision != catalogue.revision):
        raise ValidationError("Agreement assignments changed after preview. Review the current versions and preview again.")
    if RegistrarAgreementVersion.objects.filter(agreement_key=agreement_key, version=version).exists():
        raise ValidationError("That agreement version already exists. Publish a new version; historical text cannot be replaced.")
    document = RegistrarAgreementVersion.objects.create(**data, document_digest=_digest(data),
        publisher=owner, published_at=timezone.now(), submission_key=key)
    for profile in data["profile_keys"]:
        for operation in data["operations"]:
            RegistrarAgreementBinding.objects.update_or_create(agreement_key=agreement_key, profile_key=profile,
                operation=operation, defaults={"document": document})
    catalogue.revision += 1
    catalogue.save(update_fields=["revision"])
    AuditLog.objects.create(user=owner, action="domain.agreement.published", model_name="RegistrarAgreementVersion",
        object_id=str(document.pk), data={"agreement_key": agreement_key, "version": version,
            "profiles": data["profile_keys"], "operations": data["operations"], "revision": catalogue.revision})
    return document


@dataclass(frozen=True)
class AgreementPlan:
    domain_name: str
    operation: str
    profile_key: str
    documents: tuple
    manifest: tuple
    digest: str


def current_agreement_plan(domain_name, operation):
    """One joined read; no customer data, provider call or acceptance side effect."""
    if operation not in OPERATIONS or not isinstance(domain_name, str) or domain_name != domain_name.strip().lower().rstrip("."):
        raise ValidationError("Choose a normalized domain and registration/transfer operation.")
    validate_domain_name(domain_name)
    extension = tld_for_domain(domain_name)
    policy = policy_for(extension)
    profile = profile_key_for(extension)
    if operation == "transfer":
        from apps.domains.transfer_requirements import transfer_policy_for
        transfer_policy_for(extension)
    keys = policy.agreement_keys
    if (not isinstance(keys, tuple) or not 1 <= len(keys) <= 20
            or any(not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", key) for key in keys)
            or len(set(keys)) != len(keys)):
        raise ValidationError("This profile's agreement requirements need review.")
    bindings = list(RegistrarAgreementBinding.objects.filter(agreement_key__in=keys, profile_key=profile,
        operation=operation).select_related("document").order_by("agreement_key")[:21])
    if len(bindings) != len(keys):
        raise ValidationError("The required agreements have not been published for this domain workflow. Contact support.")
    documents, manifest = [], []
    for binding in bindings:
        document = binding.document
        if (document.document_digest != _digest(_document_data(document))
                or document.agreement_key != binding.agreement_key
                or not isinstance(document.profile_keys, list) or profile not in document.profile_keys
                or not isinstance(document.operations, list) or operation not in document.operations
                or document.published_at > timezone.now()):
            raise ValidationError("The published agreement record requires owner review.")
        documents.append(document)
        manifest.append({"id": document.pk, "key": document.agreement_key, "version": document.version,
                         "digest": document.document_digest})
    digest = _digest({"domain": domain_name, "operation": operation, "profile": profile, "documents": manifest})
    return AgreementPlan(domain_name, operation, profile, tuple(documents), tuple(manifest), digest)


@transaction.atomic
def record_agreement_acceptance(*, actor, contact_id, domain_name, operation, key, subject_digest,
                                plan_digest, accepted=False):
    customer = User.objects.select_for_update().get(pk=actor.pk)
    if not customer.is_active or customer.is_staff or customer.is_superuser:
        raise PermissionDenied
    contact = DomainContact.objects.select_for_update().filter(pk=contact_id, user=customer).first()
    if contact is None:
        raise PermissionDenied
    if accepted is not True:
        raise ValidationError("Explicitly accept the displayed agreements before recording acceptance.")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in (subject_digest, plan_digest)):
        raise ValidationError("Reload the agreement page before accepting.")
    if ContactVerification.digest_for(contact) != subject_digest:
        raise ValidationError("The contact changed. Review the current contact and agreements again.")
    _catalogue_lock()
    identifier = _uuid(key)
    previous = DomainAgreementAcceptance.objects.filter(key=identifier).first()
    if previous:
        if (previous.customer_id != customer.pk or previous.contact_id != contact.pk or previous.domain_name != domain_name
                or previous.operation != operation or previous.subject_digest != subject_digest or previous.plan_digest != plan_digest):
            raise ValidationError("This acceptance key belongs to another request.")
        return previous  # Does not assert that an older receipt still meets current requirements.
    plan = current_agreement_plan(domain_name, operation)
    if plan.digest != plan_digest:
        raise ValidationError("The agreements changed. Read the current versions before accepting.")
    contact.as_resellerclub_payload("1", tld_for_domain(domain_name))
    receipt = DomainAgreementAcceptance.objects.create(key=identifier, customer=customer, contact=contact,
        domain_name=domain_name, operation=operation, subject_digest=subject_digest, plan_digest=plan.digest, accepted_at=timezone.now())
    DomainAgreementAcceptanceDocument.objects.bulk_create([
        DomainAgreementAcceptanceDocument(acceptance=receipt, document=document, document_digest=document.document_digest)
        for document in plan.documents])
    AuditLog.objects.create(user=customer, action="domain.agreement.accepted", model_name="DomainAgreementAcceptance",
        object_id=str(receipt.pk), data={"document_ids": [document.pk for document in plan.documents], "operation": operation})
    return receipt


@transaction.atomic
def assert_acceptance_current(*, receipt_id, customer_id, contact_id, domain_name, operation):
    """Validate explicit evidence for integration into the invoice/paid boundary."""
    customer = User.objects.select_for_update().get(pk=customer_id)
    contact = DomainContact.objects.select_for_update().filter(pk=contact_id, user_id=customer_id).first()
    if not customer.is_active or customer.is_staff or customer.is_superuser or contact is None:
        raise ValidationError("The customer or registrant contact requires review before agreement acceptance can be used.")
    _catalogue_lock()
    plan = current_agreement_plan(domain_name, operation)
    receipt = DomainAgreementAcceptance.objects.filter(pk=receipt_id, customer_id=customer_id, contact_id=contact_id,
        domain_name=domain_name, operation=operation).first()
    if (receipt is None or receipt.subject_digest != ContactVerification.digest_for(contact)
            or receipt.plan_digest != plan.digest or receipt.accepted_at > timezone.now()):
        raise ValidationError("Current customer acceptance of the applicable agreements is required.")
    recorded = set(receipt.documents.values_list("document_id", "document_digest"))
    if (recorded != {(document.pk, document.document_digest) for document in plan.documents}
            or any(document.published_at > receipt.accepted_at for document in plan.documents)):
        raise ValidationError("The agreement acceptance record requires review.")
    return {"acceptance_id": receipt.pk, "plan_digest": receipt.plan_digest, "subject_digest": receipt.subject_digest,
            "profile_key": plan.profile_key, "documents": list(plan.manifest)}


def assert_acceptance_frozen(*, snapshot, receipt_id, customer_id, contact, domain_name, operation, admitted_at):
    """Validate admitted immutable evidence without locking or reading the current catalogue.

    The caller holds the request, customer and contact locks. Publication cutover is
    the durable submitted-at marker; later publication cannot rewrite that request.
    """
    invalid = "The admitted agreement evidence requires review before provider submission."
    if (not isinstance(snapshot, dict)
            or set(snapshot) != {"acceptance_id", "plan_digest", "subject_digest", "profile_key", "documents"}
            or type(snapshot.get("acceptance_id")) is not int or snapshot["acceptance_id"] != receipt_id
            or contact.user_id != customer_id or admitted_at is None or admitted_at > timezone.now()
            or not isinstance(snapshot.get("profile_key"), str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", snapshot["profile_key"])
            or not isinstance(snapshot.get("documents"), list) or not 1 <= len(snapshot["documents"]) <= 20):
        raise ValidationError(invalid)
    receipt = DomainAgreementAcceptance.objects.filter(pk=receipt_id, customer_id=customer_id, contact_id=contact.pk,
        domain_name=domain_name, operation=operation).first()
    if (receipt is None or receipt.accepted_at > admitted_at
            or receipt.plan_digest != snapshot["plan_digest"] or receipt.subject_digest != snapshot["subject_digest"]
            or receipt.subject_digest != ContactVerification.digest_for(contact)):
        raise ValidationError(invalid)
    links = list(receipt.documents.select_related("document").order_by("document__agreement_key"))
    manifest = []
    for link in links:
        document = link.document
        if (not agreement_document_is_intact(document) or document.published_at > receipt.accepted_at
                or link.document_digest != document.document_digest
                or snapshot["profile_key"] not in document.profile_keys or operation not in document.operations):
            raise ValidationError(invalid)
        manifest.append({"id": document.pk, "key": document.agreement_key, "version": document.version,
                         "digest": document.document_digest})
    if (manifest != snapshot["documents"] or len({row["key"] for row in manifest}) != len(manifest)
            or _digest({"domain": domain_name, "operation": operation, "profile": snapshot["profile_key"],
                        "documents": manifest}) != receipt.plan_digest):
        raise ValidationError(invalid)
    return snapshot
