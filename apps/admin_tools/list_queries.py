"""One filtered table result for display and export, starting from authorized rows."""
import csv
import io
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db.models import Q, F
from django.http import HttpResponse
from django.utils import timezone
from .list_preferences import validate_configuration

EXPORT_LIMIT = 1000


def apply_configuration(queryset, resource, configuration):
    config = validate_configuration(resource, configuration)
    filters = config["filters"]
    q = filters.get("q")
    fields = {
        "customers": ("email", "first_name", "last_name"),
        "domains": ("name", "user__email", "status"),
        "invoices": ("number", "user__email", "billing_name"),
        "tickets": ("subject", "user__email", "status"),
    }[resource]
    if q:
        expression = Q()
        for field in fields:
            expression |= Q(**{field + "__icontains": q})
        queryset = queryset.filter(expression)
    if "status" in filters:
        queryset = queryset.filter(status=filters["status"])
    if "active" in filters:
        queryset = queryset.filter(is_active=filters["active"] == "yes")
    preset = config["preset"]
    today = timezone.localdate()
    if resource == "customers" and preset in ("active", "inactive"):
        queryset = queryset.filter(is_active=preset == "active")
    elif resource == "domains" and preset == "expiring":
        queryset = queryset.filter(expires_at__gte=today, expires_at__lte=today + timedelta(days=30))
    elif resource == "invoices" and preset == "overdue":
        queryset = queryset.filter(status__in=("unpaid", "overdue", "partially_paid"), due_date__lt=today, total__gt=F("amount_paid"))
    elif resource == "tickets" and preset == "open":
        queryset = queryset.exclude(status__in=("resolved", "closed"))
    return queryset.order_by(config["sort"], "pk")


def row_values(resource, obj):
    """Explicit safe display/export projection; never arbitrary getattr traversal."""
    if resource == "customers":
        profile = getattr(obj, "business_profile", None)
        company = " / ".join(filter(None, (profile.company_name, profile.company_number))) if profile else ""
        return {"email": obj.email, "name": obj.full_name, "company": company, "active": "Yes" if obj.is_active else "No", "created": obj.created_at.isoformat()}
    if resource == "domains":
        return {"domain": obj.name, "customer": obj.user.email, "status": obj.get_status_display(), "registrant": obj.registrant_validation_status, "expiry": obj.expires_at.isoformat() if obj.expires_at else ""}
    if resource == "invoices":
        return {"number": obj.number, "customer": obj.user.email, "source": obj.get_source_kind_display(), "status": obj.get_status_display(), "total": f"{obj.currency} {obj.total:.2f}", "due": obj.due_date.isoformat() if obj.due_date else "", "created": obj.created_at.isoformat()}
    if resource == "tickets":
        return {"subject": obj.subject, "customer": obj.user.email, "status": obj.get_status_display(), "department": str(obj.department) if obj.department_id else "", "assigned": obj.assigned_to.email if obj.assigned_to_id else "", "created": obj.created_at.isoformat()}
    raise ValidationError("Unsupported table.")


def spreadsheet_cell(value):
    text = str(value)
    # Prefix risky cells even when a formula is preceded by whitespace/control characters.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        text = "'" + text
    return text


def export_csv(queryset, resource, configuration):
    config = validate_configuration(resource, configuration)
    rows = list(apply_configuration(queryset, resource, config)[:EXPORT_LIMIT + 1])
    if len(rows) > EXPORT_LIMIT:
        raise ValidationError(f"Export is limited to {EXPORT_LIMIT} rows. Narrow the filters and try again.")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([column.replace("_", " ").title() for column in config["columns"]])
    for obj in rows:
        values = row_values(resource, obj)
        writer.writerow([spreadsheet_cell(values[column]) for column in config["columns"]])
    response = HttpResponse(output.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{resource}.csv"'
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
