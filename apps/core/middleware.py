"""Custom middleware for Grumpy Hosting portal."""
import logging
import uuid

from django.conf import settings

from apps.domains.debug_state import reset_entries

logger = logging.getLogger(__name__)


class RequestCorrelationIDMiddleware:
    """
    Attach a unique X-Request-ID to every request/response pair.

    The ID is injected into:
    - The Django log record via ``logging.LoggerAdapter`` (available via
      ``request.correlation_id`` for use in views/tasks).
    - The HTTP response as ``X-Request-ID`` so it can be correlated with
      upstream load-balancer and CDN logs.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Accept an existing ID from a trusted upstream (e.g. load balancer)
        # otherwise generate a new one.
        correlation_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.correlation_id = correlation_id

        response = self.get_response(request)
        response["X-Request-ID"] = correlation_id
        return response


class ContentSecurityPolicyMiddleware:
    """
    Build and attach a ``Content-Security-Policy`` header from the
    CSP_* lists defined in settings.

    This middleware intentionally reads from settings on every request so
    that the policy can be patched without a restart during development.
    """

    _DIRECTIVE_MAP = {
        "CSP_DEFAULT_SRC": "default-src",
        "CSP_SCRIPT_SRC": "script-src",
        "CSP_STYLE_SRC": "style-src",
        "CSP_IMG_SRC": "img-src",
        "CSP_FONT_SRC": "font-src",
        "CSP_CONNECT_SRC": "connect-src",
        "CSP_FRAME_ANCESTORS": "frame-ancestors",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        directives = []
        for setting_key, directive_name in self._DIRECTIVE_MAP.items():
            sources = getattr(settings, setting_key, None)
            if sources:
                directives.append(f"{directive_name} {' '.join(sources)}")

        if directives:
            response["Content-Security-Policy"] = "; ".join(directives)

        return response


class ResellerClubDebugCaptureMiddleware:
    """Reset in-memory ResellerClub debug capture at the start of each request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        reset_entries()
        return self.get_response(request)
