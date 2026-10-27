"""Owner-reviewed assignment of the predefined least-privilege staff roles."""
import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render

from apps.accounts.models import User
from apps.audit.models import AuditLog
from .access import CAPABILITIES, ROLE_CAPABILITIES
from .decorators import staff_member_required


class StaffRoleForm(forms.Form):
    user = forms.ModelChoiceField(queryset=User.objects.none(), label="Staff member")
    roles = forms.MultipleChoiceField(choices=(), required=False, widget=forms.CheckboxSelectMultiple, label="Operational roles")
    reason = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"rows": 3}), label="Reason for this change")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = User.objects.filter(is_staff=True, is_superuser=False, is_active=True).order_by("email")
        self.fields["roles"].choices = [(name, name.removeprefix("Portal ")) for name in ROLE_CAPABILITIES]


@staff_member_required
def staff_roles(request):
    form = StaffRoleForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            target = User.objects.select_for_update().get(pk=form.cleaned_data["user"].pk)
            if target.pk == request.user.pk or target.is_superuser or not target.is_staff or not target.is_active:
                raise PermissionDenied("Owner accounts and your own access cannot be changed here.")
            before = list(target.groups.filter(name__in=ROLE_CAPABILITIES).values_list("name", flat=True))
            desired = set(form.cleaned_data["roles"])
            groups = list(Group.objects.filter(name__in=ROLE_CAPABILITIES))
            if {group.name for group in groups} != set(ROLE_CAPABILITIES):
                form.add_error(None, "Staff roles are not initialized. Apply the application migrations first.")
            else:
                target.groups.remove(*(group for group in groups if group.name not in desired))
                target.groups.add(*(group for group in groups if group.name in desired))
                target.session_version = uuid.uuid4()
                target.save(update_fields=["session_version"])
                AuditLog.objects.create(
                    user=request.user, action="staff.roles.changed", model_name="accounts.User", object_id=str(target.pk),
                    data={"before": before, "after": sorted(desired), "reason": form.cleaned_data["reason"]},
                )
                messages.success(request, f"Roles updated for {target.email}. Their existing sessions have been revoked.")
                return redirect("admin_tools:staff_roles")
    staff = User.objects.filter(is_staff=True).prefetch_related("groups").order_by("email")
    return render(request, "admin_tools/security/staff_roles.html", {
        "form": form, "staff_accounts": staff,
        "role_definitions": [{"name": name, "capabilities": sorted(dict(CAPABILITIES)[code] for code in codes)} for name, codes in ROLE_CAPABILITIES.items()],
    })
