from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from .models import Payment


@login_required
def payment_list(request):
    payments = Payment.objects.filter(user=request.user)
    return render(request, "payments/list.html", {"payments": payments})
