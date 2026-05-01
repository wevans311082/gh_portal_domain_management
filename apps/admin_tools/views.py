from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from apps.accounts.models import User
from apps.services.models import Service
from apps.billing.models import Invoice


@staff_member_required
def dashboard(request):
    context = {
        "total_users": User.objects.count(),
        "active_services": Service.objects.filter(status="active").count(),
        "unpaid_invoices": Invoice.objects.filter(status="unpaid").count(),
    }
    return render(request, "admin_tools/dashboard.html", context)
