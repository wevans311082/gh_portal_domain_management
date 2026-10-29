import pytest
from django.core.exceptions import ValidationError

from apps.billing.models import Invoice
from apps.domains.models import ContactVerification, Domain
from apps.domains.registry_requirements import validate_renewal_registrant
from apps.portal.cart_service import add_domain_renewal_item, CartPreflightError
from tests.test_contact_change_admission import current
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


@pytest.fixture(params=["me.uk", "us", "eu"])
def registry_source(current, request, monkeypatch):
    from apps.domains import registrar_policy
    extension = request.param
    if extension == "eu":
        monkeypatch.setitem(registrar_policy.POLICIES, "eu", registrar_policy.EU)
    domain, source = current
    domain.name, domain.tld = "shared-renewal." + extension, extension
    domain.save()
    source.domain_name, source.tld = domain.name, extension
    contact = source.registration_contact
    if extension == "us":
        contact.us_nexus_category, contact.us_application_purpose, contact.us_nexus_certified = "C11", "P3", True
    elif extension == "eu":
        contact.country = "IE"
        contact.eu_eligibility_basis, contact.eu_eligibility_certified = "resident", True
    contact.save()
    if extension != "us":
        source.registrar_contact_ids.update(admin="-1", tech="-1", billing="-1")
    source.save()
    attempt = source.registration_attempt
    attempt.subject = {"contacts": {str(contact.pk): ContactVerification.digest_for(contact)}}
    attempt.role_ids = dict(source.registrar_contact_ids)
    attempt.save()
    return domain, source, attempt


def test_shared_renewal_accepts_current_captured_individual_without_identity_or_business_evidence(registry_source):
    domain, source, attempt = registry_source
    validate_renewal_registrant(domain)
    assert not ContactVerification.objects.exists()
    assert not source.registration_contact.company_number


@pytest.mark.parametrize("damage", ["missing_digest", "missing_roles", "unaccepted_attempt", "wrong_remote_order", "edited_contact"])
def test_shared_renewal_rejects_unproven_or_stale_capture(registry_source, damage):
    domain, source, attempt = registry_source
    if damage == "missing_digest":
        attempt.subject = {}
    elif damage == "missing_roles":
        attempt.role_ids = {}
    elif damage == "unaccepted_attempt":
        attempt.status = "unknown"
    elif damage == "wrong_remote_order":
        attempt.remote_order_id = "9999"
    else:
        contact = source.registration_contact
        contact.address_line1 = "2 Changed Street"
        contact.save()
    attempt.save()
    with pytest.raises(ValidationError):
        validate_renewal_registrant(domain)


@pytest.mark.parametrize("field,value", [("registrar_id", "9999"), ("status", "cancelled")])
def test_shared_renewal_rereads_the_domain_instead_of_trusting_a_cached_instance(registry_source, field, value):
    domain, source, attempt = registry_source
    Domain.objects.filter(pk=domain.pk).update(**{field: value})
    with pytest.raises(ValidationError, match="domain or customer changed"):
        validate_renewal_registrant(domain)


def test_inactive_customer_cannot_pass_captured_contact_review(registry_source):
    domain, source, attempt = registry_source
    source.user.is_active = False
    source.user.save()
    with pytest.raises(ValidationError, match="domain or customer changed"):
        validate_renewal_registrant(domain)


@pytest.mark.parametrize("extension", ["eu", "unknown", "ca", "ltd.uk"])
def test_unreviewed_or_prepared_policy_cannot_create_renewal_invoice(order, extension):
    domain = Domain.objects.create(user=order.user, name="held-renewal." + extension, tld=extension,
        registrar_id="501", status="active")
    with pytest.raises(CartPreflightError, match="eligibility review"):
        add_domain_renewal_item(user=order.user, domain_id=domain.pk)
    assert not Invoice.objects.exists()
