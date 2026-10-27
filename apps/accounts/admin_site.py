"""Use the same password/MFA flow for Django admin and the customer portal."""
from urllib.parse import urlencode
from functools import wraps

from django.contrib.admin.apps import AdminConfig
from django.contrib.admin.sites import AdminSite
from django.shortcuts import redirect
from django.urls import reverse


class SecureAdminConfig(AdminConfig):
    default_site = "apps.accounts.admin_site.SecureAdminSite"


class SecureAdminSite(AdminSite):
    def has_permission(self, request):
        from apps.admin_tools.access import is_owner
        return is_owner(request.user)

    def admin_view(self, view, cacheable=False):
        admin_view = super().admin_view(view, cacheable=cacheable)
        @wraps(view)
        def protected(request, *args, **kwargs):
            from apps.admin_tools.access import is_owner, require_recent_mfa
            from django.core.exceptions import PermissionDenied
            if request.user.is_authenticated:
                if not is_owner(request.user):
                    raise PermissionDenied("Django admin is restricted to platform owners.")
                response = require_recent_mfa(request)
                if response is not None:
                    return response
            return admin_view(request, *args, **kwargs)
        return protected

    def login(self, request, extra_context=None):
        if self.has_permission(request):
            return redirect("admin:index")
        next_url = request.GET.get("next") or request.POST.get("next") or reverse("admin:index")
        return redirect(f"{reverse('account_login')}?{urlencode({'next': next_url})}")
