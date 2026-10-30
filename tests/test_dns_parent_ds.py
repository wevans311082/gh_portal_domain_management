import base64
from datetime import datetime
from unittest.mock import Mock

import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.rdatatype
import dns.rrset
import pytest
import requests
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.domains.dns_parent_ds import read_parent_ds, RESOLVERS


def denial(query, parent=None, ttl=300, expired=False):
    response = dns.message.make_response(query)
    response.flags |= dns.flags.AD | dns.flags.RA
    owner = query.question[0].name.to_text()
    parent = parent or query.question[0].name.parent().to_text()
    epoch = int(timezone.now().timestamp())
    expiry = epoch - 1 if expired else epoch + 3600
    soa = dns.rrset.from_text(parent, ttl, "IN", "SOA", "ns1.example.test. hostmaster.example.test. 1 3600 600 86400 300")
    nsec = dns.rrset.from_text(owner, ttl, "IN", "NSEC", "zzz." + parent + " NS RRSIG NSEC")
    sigs = [dns.rrset.from_text(name, ttl, "IN", "RRSIG",
        f"{kind} 13 {len(dns.name.from_text(name).labels) - 1} 300 {expiry} {epoch - 3600} 12345 {parent} AA==")
        for name, kind in ((parent, "SOA"), (owner, "NSEC"))]
    response.authority = [soa, nsec, *sigs]
    return response


class Transport:
    def __init__(self, mutate=None, headers=None, status=200):
        self.mutate, self.overrides, self.status = mutate, headers or {}, status
        self.calls, self.sessions, self.responses = [], [], []

    def session(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = self.get
        self.sessions.append(session)
        return session

    def get(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        encoded = kwargs["params"]["dns"]
        query = dns.message.from_wire(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        message = denial(query)
        if self.mutate:
            message = self.mutate(message, query, len(self.calls))
        wire = message.to_wire() if hasattr(message, "to_wire") else message
        response = Mock(status_code=self.status, headers={"Content-Type": "application/dns-message", **self.overrides})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = iter([wire])
        self.responses.append(response)
        return response


def test_two_fixed_validators_require_authenticated_ds_denial_and_bound_transport():
    transport = Transport(headers={"Age": "60"})
    result = read_parent_ds("EXAMPLE.COM.", session_factory=transport.session)
    assert result["name"] == "example.com." and result["parent_zone"] == "com."
    assert result["parent_ds_verified"] is True and result["ds_transition_verified"] is False
    assert result["ready_for_mutation"] is False and len(result["observations"]) == 2
    assert datetime.fromisoformat(result["verified_until"]) > timezone.now()
    assert all(0 < item["negative_ttl"] <= 240 for item in result["observations"])
    assert [call[0] for call in transport.calls] == [item[1] for item in RESOLVERS]
    for session, (_, args) in zip(transport.sessions, transport.calls):
        assert session.trust_env is False and session.verify is True
        assert args["timeout"] == (10, 30) and args["allow_redirects"] is False and args["stream"] is True
        assert args["verify"] is True
        query = dns.message.from_wire(base64.urlsafe_b64decode(args["params"]["dns"] + "=" * (-len(args["params"]["dns"]) % 4)))
        assert query.question[0].rdtype == dns.rdatatype.DS and query.ednsflags & dns.flags.DO
        assert query.flags & dns.flags.AD and not query.flags & dns.flags.CD


@pytest.mark.parametrize("fault", ["unauthenticated", "checking-disabled", "truncated", "servfail", "nxdomain", "answer-ds",
    "wrong-id", "no-soa", "child-soa", "no-proof", "no-signature", "expired-signature", "zero-ttl", "wrong-parent"])
def test_untrusted_or_incomplete_dns_data_never_means_parent_ds_absence(fault):
    def mutate(message, query, call):
        if fault == "unauthenticated":
            message.flags &= ~dns.flags.AD
        elif fault == "checking-disabled":
            message.flags |= dns.flags.CD
        elif fault == "truncated":
            message.flags |= dns.flags.TC
        elif fault in ("servfail", "nxdomain"):
            message.set_rcode(dns.rcode.SERVFAIL if fault == "servfail" else dns.rcode.NXDOMAIN)
        elif fault == "answer-ds":
            message.answer.append(dns.rrset.from_text(query.question[0].name, 300, "IN", "DS", "12345 13 2 " + "A" * 64))
        elif fault == "wrong-id":
            message.id ^= 1
        elif fault == "no-soa":
            message.authority = [rrset for rrset in message.authority if rrset.rdtype != dns.rdatatype.SOA]
        elif fault == "child-soa":
            return denial(query, parent=query.question[0].name.to_text())
        elif fault == "wrong-parent":
            return denial(query, parent="other.test.")
        elif fault == "no-proof":
            message.authority = [rrset for rrset in message.authority if rrset.rdtype != dns.rdatatype.NSEC]
        elif fault == "no-signature":
            message.authority = [rrset for rrset in message.authority if rrset.rdtype != dns.rdatatype.RRSIG]
        elif fault == "expired-signature":
            return denial(query, expired=True)
        else:
            return denial(query, ttl=0)
        return message
    with pytest.raises(ValidationError):
        read_parent_ds("example.com", session_factory=Transport(mutate).session)


@pytest.mark.parametrize("headers,status", [({"Content-Type": "text/html"}, 200), ({}, 302), ({}, 503),
    ({"Content-Length": "65536"}, 200), ({"Age": "300"}, 200), ({"Age": "-1"}, 200), ({"Age": "9" * 100}, 200)])
def test_http_error_redirect_and_expired_or_oversized_evidence_is_rejected(headers, status):
    transport = Transport(headers=headers, status=status)
    with pytest.raises(ValidationError):
        read_parent_ds("example.com", session_factory=transport.session)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("wire", [b"not-dns", b"x" * 65536], ids=["malformed", "oversized"])
def test_wire_payload_is_bounded_and_parsed_strictly(wire):
    with pytest.raises(ValidationError):
        read_parent_ds("example.com", session_factory=Transport(lambda *args: wire).session)


def test_both_validators_must_observe_the_same_parent_zone():
    transport = Transport(lambda message, query, call: denial(query, parent="co.uk." if call == 1 else "uk."))
    with pytest.raises(ValidationError):
        read_parent_ds("example.co.uk", session_factory=transport.session)


def test_timeout_is_sanitized_and_never_retried():
    transport = Transport()
    def factory():
        session = transport.session()
        session.get.side_effect = requests.Timeout("PRIVATE upstream error")
        return session
    with pytest.raises(ValidationError) as error:
        read_parent_ds("example.com", session_factory=factory)
    assert "PRIVATE" not in str(error.value) and len(transport.sessions) == 1


@pytest.mark.parametrize("name", ["https://evil.test/path", "localhost", "bad\r\n.test", "faß.de", None])
def test_domain_input_cannot_select_an_endpoint_or_inject_query_parameters(name):
    transport = Transport()
    with pytest.raises(ValidationError):
        read_parent_ds(name, session_factory=transport.session)
    assert not transport.calls


def test_nsec3_denial_is_supported_without_locally_claiming_chain_validation():
    def nsec3(message, query, call):
        epoch = int(timezone.now().timestamp())
        owner = "0" * 32 + ".com."
        proof = dns.rrset.from_text(owner, 300, "IN", "NSEC3", "1 0 0 - " + "0" * 32 + " NS RRSIG")
        signature = dns.rrset.from_text(owner, 300, "IN", "RRSIG", f"NSEC3 13 2 300 {epoch + 3600} {epoch - 3600} 12345 com. AA==")
        message.authority = [message.authority[0], message.authority[2], proof, signature]
        return message
    result = read_parent_ds("example.com", session_factory=Transport(nsec3).session)
    assert result["parent_ds_verified"] is True and result["ready_for_mutation"] is False


def test_network_elapsed_time_consumes_proof_ttl(monkeypatch):
    moments = iter([0, 10, 10, 11])
    monkeypatch.setattr("apps.domains.dns_parent_ds.monotonic", lambda: next(moments))
    result = read_parent_ds("example.com", session_factory=Transport().session)
    assert [item["negative_ttl"] for item in result["observations"]] == [290, 299]
