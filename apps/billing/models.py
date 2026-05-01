from django.db import models
from apps.core.models import TimeStampedModel
from apps.accounts.models import User


class Invoice(TimeStampedModel):
    STATUS_DRAFT = "draft"
    STATUS_UNPAID = "unpaid"
    STATUS_PAID = "paid"
    STATUS_VOID = "void"
    STATUS_OVERDUE = "overdue"
    STATUS_PARTIALLY_PAID = "partially_paid"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_UNPAID, "Unpaid"),
        (STATUS_PAID, "Paid"),
        (STATUS_VOID, "Void"),
        (STATUS_OVERDUE, "Overdue"),
        (STATUS_PARTIALLY_PAID, "Partially Paid"),
    ]

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="invoices")
    number = models.CharField(max_length=50, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    due_date = models.DateField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    billing_name = models.CharField(max_length=255, blank=True)
    billing_address = models.TextField(blank=True)
    stripe_invoice_id = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invoice #{self.number}"

    def calculate_totals(self):
        from decimal import Decimal
        self.subtotal = sum(item.line_total for item in self.line_items.all()) or Decimal("0.00")
        self.vat_amount = (self.subtotal * self.vat_rate / 100).quantize(Decimal("0.01"))
        self.total = self.subtotal + self.vat_amount
        self.save(update_fields=["subtotal", "vat_amount", "total"])

    @property
    def amount_outstanding(self):
        return self.total - self.amount_paid


class InvoiceLineItem(TimeStampedModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="line_items")
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    def save(self, *args, **kwargs):
        self.line_total = self.quantity * self.unit_price
        super().save(*args, **kwargs)

    def __str__(self):
        return self.description
