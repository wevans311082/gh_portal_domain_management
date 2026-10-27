from django.contrib.admin.views.decorators import staff_member_required as django_staff_member_required
from django.contrib.auth import REDIRECT_FIELD_NAME
from functools import wraps


def staff_member_required(view_func=None, redirect_field_name=REDIRECT_FIELD_NAME, login_url="account_login"):
    """Enforce an explicit route policy after the active-staff login gate."""
    def decorate(view):
        @wraps(view)
        def authorized(request, *args, **kwargs):
            from .access import authorize_admin_request
            response = authorize_admin_request(request)
            if response is None:
                response = view(request, *args, **kwargs)
            response["Cache-Control"] = "no-store"
            return response

        return django_staff_member_required(
            authorized, redirect_field_name=redirect_field_name, login_url=login_url,
        )

    return decorate(view_func) if view_func else decorate
