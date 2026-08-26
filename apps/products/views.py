from django.shortcuts import redirect


def package_list(request):
    return redirect("portal:shop")


def package_detail(request, slug):
    return redirect("portal:shop")
