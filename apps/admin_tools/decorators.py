from django.contrib.admin.views.decorators import staff_member_required as django_staff_member_required
from django.contrib.auth import REDIRECT_FIELD_NAME


def staff_member_required(view_func=None, redirect_field_name=REDIRECT_FIELD_NAME, login_url="account_login"):
    """Staff gate that always uses the custom login route (MFA-capable)."""
    return django_staff_member_required(
        view_func=view_func,
        redirect_field_name=redirect_field_name,
        login_url=login_url,
    )
