"""Strict read-only zone evidence for DNS switching and uncertain-create review."""
from dataclasses import dataclass
import re

from django.core.exceptions import ValidationError

from apps.domains.registry_requirements import validate_nameservers
from .scope import assert_provider_scope
from .services import CloudflareError, CloudflareService


@dataclass(frozen=True)
class ZoneObservation:
    zone_id: str
    name: str
    nameservers: tuple[str, ...]
    status: str
    paused: bool
    provider_scope: str


def _identifier(value):
    if type(value) is not str or re.fullmatch(r"[a-fA-F0-9]{32}", value) is None:
        raise CloudflareError("The Cloudflare zone identifier requires review.")
    return value


def _zone(value, name, client, scope):
    error = "The Cloudflare zone evidence does not match this DNS request."
    if type(value) is not dict or len(value) > 100:
        raise CloudflareError(error)
    account = value.get("account")
    if (type(account) is not dict or account.get("id") != client.account_id
            or type(value.get("name")) is not str
            or CloudflareService._zone_name(value["name"]) != name
            or value.get("type") != "full" or type(value.get("status")) is not str
            or value["status"] not in {"initializing", "pending", "active"}
            or type(value.get("paused")) is not bool):
        raise CloudflareError(error)
    hosts = value.get("name_servers")
    if (type(hosts) is not list or len(hosts) > 13
            or any(type(host) is not str or len(host) > 253 for host in hosts)):
        raise CloudflareError(error)
    try:
        validate_nameservers(name, hosts)
    except ValidationError:
        raise CloudflareError("The complete Cloudflare delegation requires registry review.") from None
    return ZoneObservation(_identifier(value.get("id")), name,
        tuple(host.strip().lower().rstrip(".") for host in hosts), value["status"], value["paused"], scope)


def _envelope(payload):
    if type(payload) is not dict or len(payload) > 20 or payload.get("success") is not True:
        raise CloudflareError("The Cloudflare zone response requires review.")


def observe_zone(zone_id, *, domain_name, client, expected_scope):
    name = CloudflareService._zone_name(domain_name)
    zone_id = _identifier(zone_id)
    assert_provider_scope(expected_scope, client)
    payload = client.get_zone(zone_id)
    assert_provider_scope(expected_scope, client)
    _envelope(payload)
    result = _zone(payload.get("result"), name, client, expected_scope)
    if result.zone_id != zone_id:
        raise CloudflareError("The Cloudflare zone identifier changed. Review this DNS request.")
    return result


def find_zone(*, domain_name, client, expected_scope):
    """Return one exact zone, or absence from a complete bounded search.

    Missing/inconsistent pagination, changing page counts, wrong-scope rows or
    multiple matches require review. No branch creates or modifies a zone.
    """
    name = CloudflareService._zone_name(domain_name)
    found = None
    expected_pages = None
    expected_page_size = None
    for page in range(1, 6):
        assert_provider_scope(expected_scope, client)
        payload = client.list_zones(name, account_id=client.account_id, page=page)
        assert_provider_scope(expected_scope, client)
        _envelope(payload)
        rows, info = payload.get("result"), payload.get("result_info")
        if type(rows) is not list or len(rows) > 50 or type(info) is not dict or len(info) > 20:
            raise CloudflareError("The Cloudflare zone search is incomplete.")
        if any(type(info.get(key)) is not int for key in ("page", "per_page", "count", "total_pages")):
            raise CloudflareError("The Cloudflare pagination evidence requires review.")
        pages = info["total_pages"]
        if (info["page"] != page or not 5 <= info["per_page"] <= 50
                or info["count"] != len(rows) or len(rows) > info["per_page"]
                or not 0 <= pages <= 5 or (pages == 0 and (page != 1 or rows))
                or (pages > 0 and page > pages)
                or (expected_page_size is not None and info["per_page"] != expected_page_size)
                or (expected_pages is not None and pages != expected_pages)):
            raise CloudflareError("The Cloudflare pagination evidence is inconsistent or exceeds the review limit.")
        expected_pages = pages
        expected_page_size = info["per_page"]
        for row in rows:
            observation = _zone(row, name, client, expected_scope)
            if found is not None:
                raise CloudflareError("Multiple Cloudflare zones matched. Owner review is required.")
            found = observation
        if page >= pages:
            return found
    raise CloudflareError("The Cloudflare zone search requires review.")
