import logging
from django.utils.deprecation import MiddlewareMixin
from .models import AuditLog

logger = logging.getLogger(__name__)

# Methods and path prefixes we don't need to audit
_SKIP_METHODS = {"HEAD", "OPTIONS"}
_SKIP_PATH_PREFIXES = (
    "/static/",
    "/media/",
    "/__debug__/",
    "/favicon.ico",
)
# Only persist audit records for state-changing requests
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _get_client_ip(request) -> str:
    """Extract the real client IP, respecting X-Forwarded-For from trusted proxies."""
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        # Take the first IP in the chain (the original client)
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


class AuditLogMiddleware:
    """
    Persist an AuditLog record for every state-changing request (POST/PUT/PATCH/DELETE)
    made by an authenticated user.

    Read-only requests (GET/HEAD/OPTIONS) and static-file paths are skipped to avoid
    polluting the audit trail with noise.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Skip early if the request is not audit-worthy
        if request.method in _SKIP_METHODS:
            return response
        if any(request.path.startswith(prefix) for prefix in _SKIP_PATH_PREFIXES):
            return response
        if request.method not in _AUDIT_METHODS:
            return response
        if not getattr(request, "user", None) or not request.user.is_authenticated:
            return response

        try:
            AuditLog.objects.create(
                user=request.user,
                action=f"{request.method} {request.path}",
                ip_address=_get_client_ip(request) or None,
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
                data={
                    "status_code": response.status_code,
                    "path": request.path,
                    "method": request.method,
                },
            )
        except Exception:
            # Audit logging must never break the request/response cycle
            logger.exception("AuditLogMiddleware: failed to write audit record")

        return response
