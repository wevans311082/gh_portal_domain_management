"""Captured configuration identity for durable DNS jobs; no provider calls."""
import json
import re

from django.utils.crypto import constant_time_compare, salted_hmac

from .services import CloudflareError, CloudflareService


def provider_scope(client):
    """Require a usable captured client and fingerprint its exact account/credential.

    Credential rotation requires review of an old request, rather than silently
    switching a pending mutation to new authority. The digest contains no token.
    """
    if getattr(client, "BASE_URL", None) != CloudflareService.BASE_URL:
        raise CloudflareError("The Cloudflare endpoint requires configuration review.")
    account = CloudflareService._account_id(getattr(client, "account_id", None))
    token = getattr(client, "token", None)
    if type(token) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", token) is None:
        raise CloudflareError("A valid Cloudflare API token must be configured.")
    # Check the captured HTTP authority too: a mutated header must not send with
    # different credentials while retaining the original request's scope.
    headers = getattr(client, "headers", None)
    if (type(headers) is not dict or set(headers) != {"Authorization", "Content-Type"}
            or headers.get("Authorization") != f"Bearer {token}"
            or headers.get("Content-Type") != "application/json"):
        raise CloudflareError("The Cloudflare request authority changed. Review its configuration.")
    payload = json.dumps({"endpoint": client.BASE_URL, "account": account, "credential": token},
                         sort_keys=True, separators=(",", ":"))
    return salted_hmac("domains.dns.cloudflare.provider_scope.v1", payload, algorithm="sha256").hexdigest()


def assert_provider_scope(expected, client):
    if (type(expected) is not str or re.fullmatch(r"[a-f0-9]{64}", expected) is None
            or not constant_time_compare(expected, provider_scope(client))):
        raise CloudflareError("Cloudflare configuration changed. Review this DNS request before continuing.")
