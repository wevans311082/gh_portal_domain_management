from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from .decorators import staff_member_required
from .list_preferences import save_view
from .list_view_controls import CAPABILITIES, ROUTES, configuration_from_query, settings_error
from .models import StaffListView


@staff_member_required
@require_POST
def saved_view_action(request, resource):
    capability = CAPABILITIES.get(resource)
    if not capability:
        raise Http404
    if not request.user.has_perm("admin_tools." + capability):
        raise PermissionDenied
    if any(len(request.POST.getlist(key)) > 1 for key in request.POST if key != "columns"):
        return settings_error(request, resource, ["Repeated table setting. No changes were saved."])
    action = request.POST.get("action")
    target = reverse("admin_tools:" + ROUTES[resource])
    try:
        if action == "save":
            config = configuration_from_query(request.POST, resource)
            obj = save_view(actor=request.user, resource=resource, name=request.POST.get("name", ""), configuration=config)
            messages.success(request, "Personal view saved.")
            return redirect(target + f"?view={obj.pk}")
        if action == "delete":
            try:
                pk = int(request.POST.get("view", ""))
            except (ValueError, OverflowError):
                raise Http404
            obj = StaffListView.objects.filter(owner=request.user, resource=resource, pk=pk).first()
            if obj is None:
                raise Http404
            obj.delete()
            messages.success(request, "Personal view deleted.")
        elif action == "reset":
            # Reset display only; saved named views remain available.
            messages.success(request, "Table settings reset.")
        else:
            return settings_error(request, resource, ["Choose a supported view action."])
    except ValidationError as exc:
        return settings_error(request, resource, exc.messages)
    return redirect(target)
