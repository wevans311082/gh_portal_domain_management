"""Early request bounds and shared atomic counters for public work."""
from dataclasses import dataclass
from io import BytesIO
import json
import math
import time

from django.conf import settings
from django.core.cache import caches
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils.cache import patch_vary_headers
from django.utils.crypto import salted_hmac
from django.utils.deprecation import MiddlewareMixin

from .http import get_client_ip


@dataclass(frozen=True)
class PublicPolicy:
    methods: frozenset
    body_limit: int
    ip_limit: int
    window: int
    global_limit: int
    email_limit: int = 0


FORM = 32 * 1024
POLICIES = {
    "admin_tools:search": PublicPolicy(frozenset({"GET"}), 0, 120, 60, 1000),
    "admin_tools:search_suggestions": PublicPolicy(frozenset({"GET"}), 0, 120, 60, 1000),
    "admin_tools:customer_workspace": PublicPolicy(frozenset({"GET"}), 0, 120, 60, 1000),
    "admin_tools:customer_note": PublicPolicy(frozenset({"GET"}), 0, 120, 60, 1000),
    "admin_tools:customer_note_add": PublicPolicy(frozenset({"POST"}), 64 * 1024, 10, 60, 100),
    "admin_tools:customer_note_redact": PublicPolicy(frozenset({"POST"}), FORM, 10, 60, 100),
    "admin_tools:identity_review": PublicPolicy(frozenset({"POST"}), FORM, 10, 600, 100),
    "core:contact": PublicPolicy(frozenset({"POST"}), FORM, 5, 600, 100, 3),
    "billing_public:quote_submit": PublicPolicy(frozenset({"POST"}), 64 * 1024, 5, 600, 100, 3),
    "billing_public:quote_public_pdf": PublicPolicy(frozenset({"GET"}), 0, 5, 600, 30),
    "billing_public:quote_public_accept": PublicPolicy(frozenset({"POST"}), FORM, 10, 600, 100),
    "billing_public:quote_public_accept_continue": PublicPolicy(frozenset({"POST"}), FORM, 10, 600, 100),
    "invoices:pdf": PublicPolicy(frozenset({"GET"}), 0, 10, 600, 60),
    "accounts_custom:register": PublicPolicy(frozenset({"POST"}), FORM, 5, 600, 60, 3),
    "account_signup": PublicPolicy(frozenset({"POST"}), FORM, 5, 600, 60, 3),
    "account_reset_password": PublicPolicy(frozenset({"POST"}), FORM, 10, 600, 100, 3),
    "account_email": PublicPolicy(frozenset({"POST"}), FORM, 10, 600, 100, 3),
    "account_confirm_email": PublicPolicy(frozenset({"POST"}), FORM, 20, 600, 200),
    "account_request_login_code": PublicPolicy(frozenset({"POST"}), FORM, 10, 600, 100, 3),
    "account_confirm_login_code": PublicPolicy(frozenset({"POST"}), FORM, 20, 600, 200),
    "account_reauthenticate": PublicPolicy(frozenset({"POST"}), FORM, 20, 600, 200),
    "account_reset_password_from_key": PublicPolicy(frozenset({"POST"}), FORM, 20, 600, 200),
    "account_confirm_password_reset_code": PublicPolicy(frozenset({"POST"}), FORM, 20, 600, 200),
    "account_complete_password_reset": PublicPolicy(frozenset({"POST"}), FORM, 20, 600, 200),
    "accounts_custom:mfa_verify": PublicPolicy(frozenset({"POST"}), FORM, 20, 600, 200),
    "accounts_custom:reauthenticate": PublicPolicy(frozenset({"POST"}), FORM, 20, 600, 200),
    "account_login": PublicPolicy(frozenset({"POST"}), FORM, 60, 300, 600, 10),
    "accounts_custom:login": PublicPolicy(frozenset({"POST"}), FORM, 60, 300, 600, 10),
    "domains:check": PublicPolicy(frozenset({"GET"}), 0, 20, 60, 600),
    "domains:whois": PublicPolicy(frozenset({"GET"}), 0, 10, 60, 100),
    "domains:verification_action": PublicPolicy(frozenset({"POST"}), FORM, 10, 600, 100),
    "domains:contact_change": PublicPolicy(frozenset({"POST"}), FORM, 10, 600, 100),
    "domains:publication_withdraw": PublicPolicy(frozenset({"POST"}), FORM, 10, 600, 100),
    "domains:search": PublicPolicy(frozenset({"GET"}), 0, 20, 60, 600),
}


class AbuseLimitExceeded(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class AbuseStoreUnavailable(Exception):
    pass


def consume_limit(scope, identity, maximum, window):
    """Atomic fixed-window limit; no read/modify/write or sliding TTL reset."""
    now = time.time()
    bucket = int(now // window)
    digest = salted_hmac("public-abuse-v1", str(identity), algorithm="sha256").hexdigest()
    key = f"public-abuse:v1:{scope}:{bucket}:{digest}"
    try:
        backend = caches[settings.PUBLIC_ABUSE_CACHE_ALIAS]
        backend.add(key, 0, timeout=window + 1)
        count = backend.incr(key)
    except Exception:
        raise AbuseStoreUnavailable from None
    if count > maximum:
        raise AbuseLimitExceeded(max(1, math.ceil((bucket + 1) * window - now)))


def _deny_constant(value):
    raise ValueError


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def bounded_json(raw):
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_deny_constant)
        if not isinstance(data, dict):
            raise ValueError
        # JSON escapes can produce unpaired surrogates, and exponent notation
        # can overflow to infinity without invoking parse_constant. Reject both
        # before cache identities, database writes or document rendering.
        json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return data
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("Enter a valid JSON object without duplicate keys.") from None


class PublicEndpointBoundaryMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        name = request.resolver_match.view_name
        policy = POLICIES.get(name)
        if not policy or request.method not in policy.methods:
            return None
        scope = "login" if name in {"account_login", "accounts_custom:login"} else name.replace(":", "-")
        if name in {"account_signup", "accounts_custom:register"}:
            scope = "registration"
        if name in {"account_reset_password_from_key", "account_confirm_password_reset_code", "account_complete_password_reset"}:
            scope = "password-reset-completion"
        if name in {"billing_public:quote_public_accept", "billing_public:quote_public_accept_continue"}:
            scope = "quote-acceptance"
        if name in {"admin_tools:search", "admin_tools:search_suggestions"}:
            scope = "admin-search"
        try:
            consume_limit(scope + "-global", "all", policy.global_limit, 60)
            consume_limit(scope + "-ip", get_client_ip(request) or "unknown", policy.ip_limit, policy.window)
            if request.user.is_authenticated:
                consume_limit(scope + "-account", request.user.pk, policy.ip_limit * 2, policy.window)
            if len(request.META.get("QUERY_STRING", "")) > 2048:
                return self.error(request, "The search query is too large.", 414)
            try:
                length = int(request.META.get("CONTENT_LENGTH") or "0")
            except (TypeError, ValueError):
                return self.error(request, "Invalid request length.", 400)
            if length < 0 or length > policy.body_limit:
                return self.error(request, "The submitted data is too large.", 413)
            # Restore bounded bytes for CSRF and downstream Django form parsing.
            raw = request.body if hasattr(request, "_body") else request.read(policy.body_limit + 1)
            if len(raw) > policy.body_limit:
                return self.error(request, "The submitted data is too large.", 413)
            request._body = raw
            request._stream = BytesIO(raw)
            email = ""
            if request.method == "POST":
                if name == "billing_public:quote_submit":
                    if request.content_type != "application/json":
                        return self.error(request, "Use application/json for quote submissions.", 415)
                    try:
                        request.public_json = bounded_json(raw)
                    except ValueError as error:
                        return self.error(request, str(error), 400)
                    lead = request.public_json.get("lead")
                    email = lead.get("email", "") if isinstance(lead, dict) else ""
                else:
                    if request.content_type not in {"application/x-www-form-urlencoded", "multipart/form-data"}:
                        return self.error(request, "Use a standard form submission.", 415)
                    if request.FILES:
                        return self.error(request, "Files are not accepted by this form.", 400)
                    if any(len(request.POST.get(field, "")) > 1024 for field in ("password", "password1", "password2")):
                        return self.error(request, "Passwords must be at most 1024 characters.", 400)
                    email = request.POST.get("email", "")
            if isinstance(email, str) and email and policy.email_limit:
                if len(email) > 254:
                    return self.error(request, "Email address is too long.", 400)
                consume_limit(scope + "-email", email.strip().casefold(), policy.email_limit, policy.window)
        except AbuseLimitExceeded as error:
            response = self.error(request, "Too many requests. Please wait before trying again.", 429, retry_after=error.retry_after)
            response["Retry-After"] = str(error.retry_after)
            return response
        except AbuseStoreUnavailable:
            return self.error(request, "This service is temporarily unavailable. Please try again later.", 503)

    @staticmethod
    def error(request, message, status, retry_after=None):
        payload = {"error": message}
        if retry_after is not None:
            payload["retry_after"] = retry_after
        # Quote and HTMX clients consume JSON. Only explicit browser document
        # requests receive HTML; no context processors/DB are needed on failure.
        is_hx = request.headers.get("HX-Request") == "true"
        wants_html = "text/html" in request.headers.get("Accept", "")
        is_quote = request.resolver_match.view_name == "billing_public:quote_submit"
        if wants_html and not is_hx and not is_quote:
            response = HttpResponse(render_to_string("public/request_error.html", {
                "message": message, "status": status, "retry_after": retry_after,
                "return_path": request.path if request.path.startswith("/") and not request.path.startswith("//") else "/",
            }), status=status)
        else:
            response = JsonResponse(payload, status=status)
        if is_hx:
            response["X-Public-Request-Error"] = "1"
        patch_vary_headers(response, ["Accept", "HX-Request"])
        response["Cache-Control"] = "no-store"
        return response
