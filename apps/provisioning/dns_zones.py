"""Complete bounded WHM zone discovery; errors never mean absence."""
import dns.exception
import dns.name
import re
from django.core.exceptions import ValidationError

from apps.domains.dns_changes import backend_scope
from .whm_client import WHMClientError


def _names(payload):
    error = "The complete WHM zone list requires review."
    metadata = payload.get("metadata") if type(payload) is dict else None
    if (type(metadata) is not dict or type(metadata.get("result")) is not int or metadata["result"] != 1
            or metadata.get("command") != "listzones" or type(metadata.get("version")) is not int or metadata["version"] != 1):
        raise WHMClientError(error)
    data = payload.get("data")
    rows = data.get("zone") if type(data) is dict else None
    if type(rows) is not list or len(rows) > 5000:
        raise WHMClientError(error)
    names = set()
    for row in rows:
        value = row.get("domain") if type(row) is dict else None
        if (type(value) is not str or not value.isascii() or len(value) > 253
                or any(ord(char) <= 32 or ord(char) == 127 for char in value)
                or value != "." and any(not re.fullmatch(r"[A-Za-z0-9_*\-]{1,63}", label) for label in value.rstrip(".").split("."))):
            raise WHMClientError(error)
        try:
            name = dns.name.from_text(value, origin=dns.name.root).canonicalize()
        except dns.exception.DNSException:
            raise WHMClientError(error) from None
        if name in names:
            raise WHMClientError(error)
        names.add(name)
    return names


def zone_exists(*, domain_name, client, expected_scope):
    from django.core.validators import validate_domain_name
    validate_domain_name(domain_name)
    target = dns.name.from_text(domain_name, origin=dns.name.root).canonicalize()
    observations = []
    for _ in range(2):
        if backend_scope("cpanel", client) != expected_scope:
            raise ValidationError("The WHM zone-discovery account changed.")
        names = _names(client.list_dns_zones())
        if backend_scope("cpanel", client) != expected_scope:
            raise ValidationError("The WHM zone-discovery account changed.")
        observations.append(target in names)
    if observations[0] != observations[1]:
        raise WHMClientError("The WHM target appeared or disappeared during discovery.")
    if not observations[0] and client.username != "root":
        raise WHMClientError("WHM absence requires a complete root-account zone inventory.")
    return observations[0]
