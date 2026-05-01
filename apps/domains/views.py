from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from .models import Domain


@login_required
def domain_list(request):
    domains = Domain.objects.filter(user=request.user)
    return render(request, "domains/list.html", {"domains": domains})


@login_required
def domain_detail(request, pk):
    domain = get_object_or_404(Domain, pk=pk, user=request.user)
    return render(request, "domains/detail.html", {"domain": domain})
