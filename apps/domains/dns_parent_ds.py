"""Authenticated public DS denial through two fixed HTTPS validating resolvers.

We trust these validators over verified TLS; this is not local DNSSEC chain
validation and it does not prove that historical positive DS caches expired.
"""
import base64
from datetime import datetime, timedelta
import hashlib
from math import ceil
from time import monotonic

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.opcode
import dns.rcode
import dns.rdatatype
import requests
from django.core.exceptions import ValidationError
from django.core.validators import validate_domain_name
from django.utils import timezone

from .contact_bindings import _fingerprint


RESOLVERS = (("cloudflare", "https://cloudflare-dns.com/dns-query"), ("google", "https://dns.google/dns-query"))
MAX_WIRE_BYTES = 65535
ERROR = "Authenticated public parent DS absence could not be verified. Review DNSSEC before switching."


def _denial(query, wire, *, age, now):
    try:
        message = dns.message.from_wire(wire, ignore_trailing=False)
    except (dns.exception.DNSException, ValueError):
        raise ValidationError(ERROR) from None
    name = query.question[0].name
    if (not query.is_response(message) or message.opcode() != dns.opcode.QUERY
            or message.rcode() != dns.rcode.NOERROR or message.answer
            or any(rrset.rdtype == dns.rdatatype.DS for rrset in message.authority)
            or not message.flags & dns.flags.AD or not message.flags & dns.flags.RA
            or message.flags & (dns.flags.CD | dns.flags.TC)):
        raise ValidationError(ERROR)
    soa = [rrset for rrset in message.authority if rrset.rdtype == dns.rdatatype.SOA]
    if (len(soa) != 1 or len(soa[0]) != 1 or soa[0].rdclass != 1
            or soa[0].name == name or not name.is_subdomain(soa[0].name)):
        raise ValidationError(ERROR)
    parent = soa[0].name.canonicalize()
    proofs = [rrset for rrset in message.authority if rrset.rdtype in (dns.rdatatype.NSEC, dns.rdatatype.NSEC3)]
    if not proofs or len(message.authority) > 32:
        raise ValidationError(ERROR)
    ttls = [soa[0].ttl, soa[0][0].minimum]
    epoch = int(now.timestamp())
    for rrset in [soa[0], *proofs]:
        if (rrset.rdclass != 1 or not rrset.name.is_subdomain(parent)
                or not 0 < rrset.ttl <= 2147483647):
            raise ValidationError(ERROR)
        signatures = [(record, sigset.ttl) for sigset in message.authority
            if sigset.rdtype == dns.rdatatype.RRSIG and sigset.rdclass == 1 and sigset.name == rrset.name
            for record in sigset if record.type_covered == rrset.rdtype and record.signer.canonicalize() == parent
            and record.inception <= epoch < record.expiration and 0 < sigset.ttl <= 2147483647]
        if not signatures:
            raise ValidationError(ERROR)
        ttls.extend([rrset.ttl, min(min(record.expiration - epoch, ttl) for record, ttl in signatures)])
    remaining = min(ttls) - age
    if remaining <= 0:
        raise ValidationError(ERROR)
    return {"parent_zone": parent.to_text(), "negative_ttl": remaining,
            "authenticated": True, "state": "no_ds", "wire_digest": hashlib.sha256(wire).hexdigest()}


def _read(session, endpoint, query):
    encoded = base64.urlsafe_b64encode(query.to_wire()).rstrip(b"=").decode("ascii")
    try:
        with session.get(endpoint, params={"dns": encoded}, headers={"Accept": "application/dns-message", "Cache-Control": "no-cache"},
                         timeout=(10, 30), allow_redirects=False, stream=True, verify=True) as response:
            if response.status_code != 200 or response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/dns-message":
                raise ValidationError(ERROR)
            length, age = response.headers.get("Content-Length"), response.headers.get("Age", "0")
            if ((length is not None and (len(length) > 10 or not length.isascii() or not length.isdigit() or int(length) > MAX_WIRE_BYTES))
                    or len(age) > 10 or not age.isascii() or not age.isdigit() or int(age) > 2147483647):
                raise ValidationError(ERROR)
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=4096):
                size += len(chunk)
                if size > MAX_WIRE_BYTES:
                    raise ValidationError(ERROR)
                chunks.append(chunk)
            return b"".join(chunks), int(age)
    except requests.RequestException:
        raise ValidationError(ERROR) from None


def read_parent_ds(name, *, session_factory=None):
    """Return bounded, time-limited public denial evidence, never a write permit.

    Endpoints are fixed; domain input cannot select a URL, redirect, credential
    or transport. Production uses fresh sessions with TLS verification and no
    environment credentials/proxies. session_factory is trusted test injection.
    """
    if type(name) is not str:
        raise ValidationError(ERROR)
    name = name.lower().rstrip(".")
    if not name.isascii():
        raise ValidationError(ERROR)
    validate_domain_name(name)
    origin = dns.name.from_text(name, origin=dns.name.root).canonicalize()
    if len(origin.labels) < 3:
        raise ValidationError(ERROR)
    observations = []
    for resolver, endpoint in RESOLVERS:
        query = dns.message.make_query(origin, dns.rdatatype.DS, want_dnssec=True)
        query.flags |= dns.flags.AD
        query.flags &= ~dns.flags.CD
        started = monotonic()
        with (session_factory or requests.Session)() as session:
            session.trust_env = False
            session.verify = True
            session.headers.clear()
            wire, age = _read(session, endpoint, query)
        now = timezone.now()
        elapsed = max(0, ceil(monotonic() - started))
        observations.append({"resolver": resolver, "observed_at": now.isoformat(),
            **_denial(query, wire, age=age + elapsed, now=now)})
    if observations[0]["parent_zone"] != observations[1]["parent_zone"]:
        raise ValidationError(ERROR)
    # Time spent querying the second resolver consumes the first proof's TTL.
    until = min(datetime.fromisoformat(item["observed_at"]) + timedelta(seconds=item["negative_ttl"]) for item in observations)
    if until <= timezone.now():
        raise ValidationError(ERROR)
    receipt = {"version": 1, "name": origin.to_text(), "parent_zone": observations[0]["parent_zone"],
        "state": "no_ds", "parent_ds_verified": True, "verified_until": until.isoformat(),
        "observations": observations, "ds_transition_verified": False, "ready_for_mutation": False}
    return {**receipt, "digest": _fingerprint(receipt)}
