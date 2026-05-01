from django.contrib import admin
from .models import Package, PackageFeature


class PackageFeatureInline(admin.TabularInline):
    model = PackageFeature
    extra = 1


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ["name", "price_monthly", "price_annually", "is_active", "is_featured"]
    list_filter = ["is_active", "is_featured"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ("name",)}
    inlines = [PackageFeatureInline]
