"""Canonical client addresses, with explicit socket-peer proxy trust."""
from functools import lru_cache
from ipaddress import ip_address, ip_network

from django.conf import settings

MAX_FORWARDED_LENGTH = 4096
MAX_FORWARDED_HOPS = 32


def _address(value):
    if not isinstance(value, str) or not value.strip() or "%" in value:
        raise ValueError("Expected a plain IPv4 or IPv6 address")
    address = ip_address(value.strip())
    return getattr(address, "ipv4_mapped", None) or address


def normalize_ip(value) -> str:
    """Canonicalize stored addresses with the same rules as socket addresses."""
    try:
        return str(_address(value))
    except (ValueError, TypeError):
        return ""


@lru_cache(maxsize=32)
def _trusted_networks(values):
    if any(not isinstance(value, str) or not value.strip() or "%" in value for value in values):
        raise ValueError("Trusted proxy entries must be IPv4/IPv6 addresses or CIDRs")
    return tuple(ip_network(value.strip(), strict=True) for value in values)


def trusted_proxy_networks():
    values = getattr(settings, "TRUSTED_PROXY_CIDRS", ())
    if not isinstance(values, (list, tuple)):
        raise ValueError("TRUSTED_PROXY_CIDRS must be a list of addresses or CIDRs")
    return _trusted_networks(tuple(values))


def get_client_ip(request) -> str:
    """Walk X-Forwarded-For right-to-left only behind explicitly trusted peers.

    Stop at the first untrusted hop and ignore everything to its left. Ignore
    forwarding headers entirely for an untrusted socket peer. Invalid socket
    addresses or malformed trusted forwarding chains return an empty address,
    which cannot match an IP allowlist and uses the unknown throttle bucket.
    RFC Forwarded and X-Real-IP are deliberately not additional sources.
    """
    try:
        peer = _address(request.META.get("REMOTE_ADDR", ""))
        networks = trusted_proxy_networks()
    except (ValueError, TypeError):
        return ""

    def trusted(address):
        return any(address.version == network.version and address in network for network in networks)

    if not trusted(peer):
        return str(peer)
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if not forwarded:
        return str(peer)
    if not isinstance(forwarded, str) or len(forwarded) > MAX_FORWARDED_LENGTH:
        return ""
    parts = forwarded.split(",")
    if len(parts) > MAX_FORWARDED_HOPS:
        return ""
    try:
        hops = [_address(part) for part in parts]
    except (ValueError, TypeError):
        return ""
    current = peer
    for hop in reversed(hops):
        if not trusted(current):
            break
        current = hop
    return str(current)
