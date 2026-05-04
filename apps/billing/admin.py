from django.contrib import admin

from .models import (
    BillingDocumentBranding,
    Invoice,
    InvoiceLineItem,
    Quote,
    QuoteLineItem,
)


class InvoiceLineItemInline(admin.TabularInline):
    model = InvoiceLineItem
    extra = 1
    readonly_fields = ["line_total"]
    fields = ["position", "description", "quantity", "unit_price", "line_total"]


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ["number", "user", "status", "source_kind", "total", "currency", "due_date", "paid_at"]
    list_filter = ["status", "source_kind", "currency"]
    search_fields = ["number", "user__email", "billing_name"]
    readonly_fields = ["subtotal", "vat_amount", "total", "amount_outstanding"]
    inlines = [InvoiceLineItemInline]
    raw_id_fields = ["user", "source_quote", "created_by_staff"]
    actions = ["action_send_email"]

    @admin.action(description="Email selected invoices to recipients")
    def action_send_email(self, request, queryset):
        from apps.billing.services import email_document

        sent = 0
        for invoice in queryset:
            try:
                email_document(invoice, kind="invoice_issued")
                sent += 1
            except Exception as exc:  # pragma: no cover
                self.message_user(request, f"{invoice.number}: {exc}", level="ERROR")
        self.message_user(request, f"Emailed {sent} invoice(s).")


class QuoteLineItemInline(admin.TabularInline):
    model = QuoteLineItem
    extra = 1
    readonly_fields = ["line_total"]
    fields = ["position", "description", "quantity", "unit_price", "line_total"]


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = ["number", "display_recipient", "status", "total", "currency", "valid_until", "created_at"]
    list_filter = ["status", "currency"]
    search_fields = ["number", "user__email", "lead_email", "lead_name", "lead_company"]
    readonly_fields = ["subtotal", "vat_amount", "total", "public_token", "accepted_at", "accepted_by_ip"]
    inlines = [QuoteLineItemInline]
    raw_id_fields = ["user", "converted_invoice", "created_by"]
    actions = ["action_send_email"]

    @admin.action(description="Email selected quotes to recipients")
    def action_send_email(self, request, queryset):
        from apps.billing.services import email_document

        sent = 0
        for quote in queryset:
            try:
                email_document(quote, kind="quote_sent")
                sent += 1
            except Exception as exc:  # pragma: no cover
                self.message_user(request, f"{quote.number}: {exc}", level="ERROR")
        self.message_user(request, f"Emailed {sent} quote(s).")


@admin.register(BillingDocumentBranding)
class BillingDocumentBrandingAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Identity", {"fields": ("company_name", "registered_address", "company_number", "vat_number")}),
        ("Contact", {"fields": ("support_email", "support_phone", "website_url")}),
        ("Look & feel", {"fields": ("logo", "accent_colour")}),
        ("Document text", {"fields": ("header_text", "footer_text", "legal_text", "signature_block")}),
        ("Defaults", {"fields": ("default_currency", "default_vat_rate", "default_due_days", "default_quote_validity_days")}),
        ("Numbering", {"fields": ("invoice_number_format", "quote_number_format", "invoice_seq", "quote_seq")}),
    )

    def has_add_permission(self, request):
        if BillingDocumentBranding.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False
