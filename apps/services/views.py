from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from .models import Service


@login_required
def service_list(request):
    services = Service.objects.filter(user=request.user)
    return render(request, "services/service_list.html", {"services": services})


@login_required
def service_detail(request, pk):
    service = get_object_or_404(Service, pk=pk, user=request.user)
    return render(request, "services/service_detail.html", {"service": service})
