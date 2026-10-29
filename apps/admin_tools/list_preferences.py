"""Typed personal table preferences. Callers retain all access-control filters."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.contrib.auth import get_user_model
from .models import StaffListView


TABLES = {
    "customers": {"columns": ("email", "name", "company", "active", "created"), "sorts": ("email", "-email", "created_at", "-created_at"), "filters": ("q", "active"), "presets": ("all", "active", "inactive")},
    "domains": {"columns": ("domain", "customer", "status", "registrant", "expiry"), "sorts": ("name", "-name", "expires_at", "-expires_at"), "filters": ("q", "status"), "presets": ("all", "expiring")},
    "invoices": {"columns": ("number", "customer", "source", "status", "total", "due", "created"), "sorts": ("number", "-number", "created_at", "-created_at", "due_date", "-due_date"), "filters": ("q", "status"), "presets": ("all", "overdue")},
    "tickets": {"columns": ("subject", "customer", "status", "department", "assigned", "created"), "sorts": ("created_at", "-created_at", "subject", "-subject"), "filters": ("q", "status"), "presets": ("all", "open")},
}
DEFAULT_SORTS = {"customers": "-created_at", "domains": "name", "invoices": "-created_at", "tickets": "-created_at"}


def validate_configuration(resource, value):
    spec = TABLES.get(resource)
    if spec is None or type(value) is not dict:
        raise ValidationError("Choose a supported table and valid settings.")
    if set(value) - {"columns", "sort", "page_size", "filters", "preset"}:
        raise ValidationError("Unknown table setting.")
    columns = value.get("columns", list(spec["columns"]))
    if (type(columns) is not list or not columns or len(columns) > len(spec["columns"])
            or any(type(column) is not str or column not in spec["columns"] for column in columns)
            or len(set(columns)) != len(columns)):
        raise ValidationError("Select distinct supported columns.")
    sort = value.get("sort", DEFAULT_SORTS[resource])
    if type(sort) is not str or sort not in spec["sorts"]:
        raise ValidationError("Choose a supported sort order.")
    size = value.get("page_size", 25)
    if type(size) is not int or size not in (10, 25, 50, 100):
        raise ValidationError("Choose 10, 25, 50 or 100 rows per page.")
    filters = value.get("filters", {})
    if type(filters) is not dict or set(filters) - set(spec["filters"]):
        raise ValidationError("Unsupported filter.")
    clean_filters = {}
    for key, text in filters.items():
        if type(text) is not str or len(text) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ValidationError("Filter values must be plain text of at most 200 characters.")
        if text.strip():
            clean_filters[key] = text.strip()
    if "active" in clean_filters and clean_filters["active"] not in ("yes", "no"):
        raise ValidationError("Choose an active-account filter.")
    if "status" in clean_filters:
        from apps.domains.models import Domain
        from apps.billing.models import Invoice
        from apps.support.models import SupportTicket
        model = {"domains": Domain, "invoices": Invoice, "tickets": SupportTicket}[resource]
        if clean_filters["status"] not in dict(model.STATUS_CHOICES):
            raise ValidationError("Choose a supported status.")
    preset = value.get("preset", "all")
    if type(preset) is not str or preset not in spec["presets"]:
        raise ValidationError("Choose a supported built-in view.")
    return {"columns": columns[:], "sort": sort, "page_size": size, "filters": clean_filters, "preset": preset}


@transaction.atomic
def save_view(*, actor, resource, name, configuration):
    # Lock the owner to serialize the per-table limit, including concurrent inserts.
    actor = get_user_model()._default_manager.select_for_update().get(pk=actor.pk)
    if not actor.is_active or not actor.is_staff:
        raise ValidationError("An active staff account is required.")
    if type(name) is not str or not name.strip() or len(name) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValidationError("Use a view name of 1–80 characters.")
    configuration = validate_configuration(resource, configuration)
    views = StaffListView.objects.filter(owner=actor, resource=resource)
    existing = views.filter(name=name.strip()).first()
    if existing is None and views.count() >= 20:
        raise ValidationError("Keep at most 20 saved views for each table.")
    obj, _ = StaffListView.objects.update_or_create(owner=actor, resource=resource, name=name.strip(), defaults={"configuration": configuration})
    return obj


def load_view(*, actor, resource, pk):
    """Foreign views are indistinguishable from missing views; revalidate stored JSON."""
    obj = StaffListView.objects.get(owner=actor, resource=resource, pk=pk)
    return obj, validate_configuration(resource, obj.configuration)
