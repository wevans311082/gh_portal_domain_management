from django.contrib import admin
from .models import BusinessProfile


@admin.register(BusinessProfile)
class BusinessProfileAdmin(admin.ModelAdmin):
    list_display = ["company_name", "user", "company_number", "is_verified", "verified_at"]
    list_filter = ["is_verified", "company_type"]
    search_fields = ["company_name", "company_number", "user__email"]
