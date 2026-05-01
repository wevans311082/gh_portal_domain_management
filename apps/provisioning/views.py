from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def job_list(request):
    return render(request, "provisioning/list.html", {})
