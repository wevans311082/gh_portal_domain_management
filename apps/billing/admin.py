from django.contrib import admin
from .models import Invoice, InvoiceLineItem


class InvoiceLineItemInline(admin.TabularInline):
    model = InvoiceLineItem
    extra = 1
    readonly_fields = ["line_total"]


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ["number", "user", "status", "total", "due_date", "paid_at"]
    list_filter = ["status"]
    search_fields = ["number", "user__email"]
    readonly_fields = ["subtotal", "vat_amount", "total", "amount_outstanding"]
    inlines = [InvoiceLineItemInline]
    raw_id_fields = ["user"]
