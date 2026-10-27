from django.contrib.auth import logout
from django.core.exceptions import PermissionDenied
from django.utils.deprecation import MiddlewareMixin

from .impersonation import CONTEXT_KEY, validated_owner


class IntegrationSecretFailureMiddleware(MiddlewareMixin):
    def process_exception(self, request, exception):
        from .secrets import IntegrationSecretError
        if not isinstance(exception, IntegrationSecretError):
            return None
        import logging
        import uuid
        from django.http import JsonResponse
        reference = uuid.uuid4().hex
        logging.getLogger(__name__).error("Integration secret unavailable reference=%s", reference)
        response = JsonResponse({"error": "Integration configuration is temporarily unavailable.", "reference": reference}, status=503)
        response["Cache-Control"] = "no-store"
        return response


class ImpersonationBoundaryMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        context = request.session.get(CONTEXT_KEY)
        if context is None and not request.session.get("impersonator_user_id"):
            return None
        actor = validated_owner(request)
        if actor is None:
            logout(request)
            raise PermissionDenied("Customer impersonation expired or the owner's access changed. Sign in again.")
        request.impersonation_actor = actor
        match = request.resolver_match
        # allauth routes are deliberately unnamespaced in the root URLconf.
        # Gate their view modules too, so alternate password/email/social-account
        # management paths cannot bypass the custom account boundary.
        view_class = getattr(view_func, "view_class", None)
        module = getattr(view_class or view_func, "__module__", "")
        if module.startswith("allauth.") and match.url_name != "account_logout":
            raise PermissionDenied("Account credentials cannot be managed while impersonating a customer.")
        if match.namespace == "accounts_custom":
            if match.url_name != "profile" or request.method != "GET":
                raise PermissionDenied("Account credentials cannot be managed while impersonating a customer.")
        if match.namespace == "portal" and match.url_name in {"api_key_create", "api_key_revoke", "gdpr_data_export"}:
            raise PermissionDenied("API credentials and data exports are unavailable while impersonating.")
        if match.namespace == "payments" and match.url_name in {"add_card", "delete_card", "set_default_card"}:
            raise PermissionDenied("Saved payment methods cannot be changed while impersonating.")
