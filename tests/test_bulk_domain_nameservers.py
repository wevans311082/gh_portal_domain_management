from unittest.mock import patch
import pytest
from django.core.exceptions import ValidationError
from apps.domains.bulk_remote import execute_remote_nameservers, execute_remote_lock
from apps.domains.models import DomainOperationReservation
from tests.test_bulk_domain_admission import preview, confirm
from tests.test_bulk_domain_remote import Provider
from tests.test_registration_submission import order
from tests.test_contact_change_admission import current

pytestmark = pytest.mark.django_db(transaction=True)


class NameserverProvider(Provider):
    def modify_nameservers(self, order_id, nameservers):
        self.nameservers = list(nameservers)
        return self._send("nameservers", order_id)


def admitted_nameservers(current, count=13):
    values = [f"ns{i}.example.test" for i in range(1, count + 1)]
    batch = preview(current, action="nameservers", target={"nameservers": values})
    confirm(batch)
    return batch.items.get(), values


def test_all_thirteen_targets_are_retained_and_sent_once_without_local_success(current):
    item, values = admitted_nameservers(current)
    provider = NameserverProvider(item)
    with patch("apps.provisioning.whm_client.WHMClient.__init__", side_effect=AssertionError("Registrar submission must not imply a hosting mutation")):
        assert execute_remote_nameservers(item.pk, client=provider).status == "awaiting"
        execute_remote_nameservers(item.pk, client=provider)
    assert provider.nameservers == values and provider.calls == [("nameservers", "501")]
    assert item.batch.target["nameservers"] == values
    current[0].refresh_from_db()
    assert current[0].registrar_nameservers == []
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_nameserver_timeout_is_unknown_and_never_reposts(current):
    item, values = admitted_nameservers(current)
    provider = NameserverProvider(item, error=TimeoutError("PRIVATE-ERROR"))
    assert execute_remote_nameservers(item.pk, client=provider).status == "unknown"
    execute_remote_nameservers(item.pk, client=provider)
    assert len(provider.calls) == 1
    assert DomainOperationReservation.objects.filter(item=item).exists()


def test_complete_cache_retains_all_values_and_mirrors_legacy_fields(current):
    domain = current[0]
    values = [f"ns{i}.example.test" for i in range(1, 14)]
    domain.set_nameserver_cache(values)
    domain.save(update_fields=["registrar_nameservers", "nameserver1", "nameserver2", "nameserver3", "nameserver4"])
    domain.refresh_from_db()
    assert domain.cached_nameservers == values
    assert [getattr(domain, f"nameserver{i}") for i in range(1, 5)] == values[:4]
    domain.set_nameserver_cache(values[:2])
    assert domain.registrar_nameservers == values[:2] and domain.nameserver3 == domain.nameserver4 == ""


def test_changed_fifth_cached_nameserver_invalidates_admitted_request(current):
    domain = current[0]
    values = [f"ns{i}.example.test" for i in range(1, 6)]
    domain.set_nameserver_cache(values)
    domain.save()
    item, target = admitted_nameservers(current)
    domain.registrar_nameservers[-1] = "changed.example.test"
    domain.save(update_fields=["registrar_nameservers"])
    provider = NameserverProvider(item)
    result = execute_remote_nameservers(item.pk, client=provider)
    assert result.status == "failed" and result.submitted_at is None and not provider.calls


def test_legacy_cache_fallback_does_not_invent_full_or_verified_values(current):
    domain = current[0]
    domain.nameserver1, domain.nameserver2 = "ns1.legacy.test", "ns2.legacy.test"
    assert domain.cached_nameservers == ["ns1.legacy.test", "ns2.legacy.test"]
    assert domain.registrar_nameservers == []
    with pytest.raises(ValidationError):
        domain.set_nameserver_cache(["bad host", "bad host"])


def test_wrong_executor_cannot_change_an_admitted_nameserver_request(current):
    item, target = admitted_nameservers(current)
    provider = NameserverProvider(item)
    with pytest.raises(ValidationError, match="executor matching"):
        execute_remote_lock(item.pk, client=provider)
    assert not provider.calls and DomainOperationReservation.objects.filter(item=item).exists()
