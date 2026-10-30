import pytest
from django.core.exceptions import ValidationError
from apps.domains.bulk_observation import observed_nameservers
from apps.domains.bulk_reconciliation import observe_remote_nameservers
from apps.domains.bulk_remote import execute_remote_nameservers
from apps.domains.models import DomainOperationReservation
from tests.test_bulk_domain_nameservers import admitted_nameservers, NameserverProvider
from tests.test_bulk_domain_observation import Reader, evidence
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db(transaction=True)


def delegation(item, values):
    payload = evidence(item, [])
    payload["noOfNameServers"] = len(values)
    payload.update({f"ns{i}": value for i, value in enumerate(values, 1)})
    return payload


def test_all_thirteen_hosts_are_matched_and_retained_without_completion(current):
    item, values = admitted_nameservers(current)
    execute_remote_nameservers(item.pk, client=NameserverProvider(item))
    reader = Reader(item, delegation(item, list(reversed(values))))
    result = observe_remote_nameservers(item.pk, client=reader)
    assert result.observed_nameservers == list(reversed(values))
    assert result.status == "awaiting" and result.completed_at is None
    assert DomainOperationReservation.objects.filter(item=item).exists()
    current[0].refresh_from_db()
    assert current[0].registrar_nameservers == []
    observe_remote_nameservers(item.pk, client=reader)
    assert reader.reads == ["501"]


@pytest.mark.parametrize("field,value", [("noOfNameServers", None), ("noOfNameServers", True), ("noOfNameServers", 14), ("noOfNameServers", 0), ("noOfNameServers", "２"), ("ns13", None), ("ns13", "bad host"), ("ns14", "extra.example.test"), ("ns01", "alias.example.test"), ("ns999999", "extra.example.test"), ("customerid", "9999"), ("techcontactid", "9999")])
def test_missing_extra_or_ambiguous_delegation_cannot_be_accepted(current, field, value):
    item, values = admitted_nameservers(current)
    payload = delegation(item, values)
    payload[field] = value
    with pytest.raises(ValidationError):
        observed_nameservers(payload, item.snapshot)


def test_normalization_rejects_duplicate_hosts_and_does_not_accept_a_prefix(current):
    item, values = admitted_nameservers(current)
    payload = delegation(item, values)
    payload["ns13"] = "NS1.EXAMPLE.TEST."
    with pytest.raises(ValidationError):
        observed_nameservers(payload, item.snapshot)
    payload = delegation(item, values[:4])
    assert observed_nameservers(payload, item.snapshot) == values[:4]
    execute_remote_nameservers(item.pk, client=NameserverProvider(item))
    result = observe_remote_nameservers(item.pk, client=Reader(item, payload))
    assert result.status == "awaiting" and result.observed_nameservers == values[:4]
    assert "does not yet report" in result.message
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_changed_fifth_host_during_read_invalidates_the_observation(current):
    item, values = admitted_nameservers(current)
    execute_remote_nameservers(item.pk, client=NameserverProvider(item))
    def changed():
        current[0].set_nameserver_cache([f"old{i}.example.test" for i in range(1, 6)])
        current[0].save(update_fields=["registrar_nameservers", "nameserver1", "nameserver2", "nameserver3", "nameserver4"])
    result = observe_remote_nameservers(item.pk, client=Reader(item, delegation(item, values), hook=changed))
    assert result.status == "unknown" and result.observed_nameservers is None
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_uk_evidence_requires_only_the_applicable_registrant_role(current):
    domain, source = current
    domain.name, domain.tld = "delegation-fixture.co.uk", "co.uk"
    domain.save(update_fields=["name", "tld"])
    source.domain_name, source.tld = domain.name, domain.tld
    source.save(update_fields=["domain_name", "tld"])
    item, values = admitted_nameservers(current, count=2)
    payload = delegation(item, values)
    for key in ("admincontactid", "techcontactid", "billingcontactid"):
        payload.pop(key)
    assert observed_nameservers(payload, item.snapshot) == values
