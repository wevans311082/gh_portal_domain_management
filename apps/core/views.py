from django.shortcuts import render
from django.http import JsonResponse
from django.db import connection
from django.core.cache import cache


def health_check(request):
    status = {"status": "ok", "database": "ok", "cache": "ok"}
    http_status = 200

    try:
        connection.ensure_connection()
    except Exception as e:
        status["database"] = f"error: {str(e)}"
        status["status"] = "degraded"
        http_status = 503

    try:
        cache.set("health_check", "ok", 30)
        cache.get("health_check")
    except Exception as e:
        status["cache"] = f"error: {str(e)}"
        status["status"] = "degraded"
        http_status = 503

    return JsonResponse(status, status=http_status)


def home(request):
    return render(request, "public/home.html")


def pricing(request):
    from apps.products.models import Package
    packages = Package.objects.filter(is_active=True).order_by("price_monthly")
    return render(request, "public/pricing.html", {"packages": packages})


def contact(request):
    return render(request, "public/contact.html")


def handler404(request, exception):
    return render(request, "404.html", status=404)


def handler500(request):
    return render(request, "500.html", status=500)
