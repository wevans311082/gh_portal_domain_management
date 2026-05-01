from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def invoice_list(request):
    from apps.billing.models import Invoice
    invoices = Invoice.objects.filter(user=request.user)
    return render(request, "invoices/list.html", {"invoices": invoices})
