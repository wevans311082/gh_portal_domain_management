from django.shortcuts import render, get_object_or_404
from .models import Package


def package_list(request):
    packages = Package.objects.filter(is_active=True)
    return render(request, "products/package_list.html", {"packages": packages})


def package_detail(request, slug):
    package = get_object_or_404(Package, slug=slug, is_active=True)
    return render(request, "products/package_detail.html", {"package": package})
