from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def dashboard(request):
    user = request.user
    context = {
        "services": user.services.all()[:5] if hasattr(user, "services") else [],
        "invoices": user.invoices.filter(status__in=["unpaid", "overdue"])[:5] if hasattr(user, "invoices") else [],
        "domains": user.domains.all()[:5] if hasattr(user, "domains") else [],
        "tickets": user.support_tickets.filter(status__in=["open", "awaiting_support"])[:5] if hasattr(user, "support_tickets") else [],
    }
    return render(request, "portal/dashboard.html", context)
