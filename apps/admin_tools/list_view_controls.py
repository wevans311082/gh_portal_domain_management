"""Request adapter shared by existing authorized list routes."""
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from .list_preferences import TABLES, load_view, validate_configuration
from .list_queries import apply_configuration, export_csv, row_values
from django.urls import reverse
from django.shortcuts import render
from urllib.parse import urlencode
from .models import StaffListView

ROUTES = {"customers": "users", "domains": "domains_list", "invoices": "invoice_list", "tickets": "tickets_list"}
CAPABILITIES = {"customers": "view_customers", "domains": "view_domains", "invoices": "view_billing", "tickets": "view_support"}


def configuration_from_query(data, resource, base=None):
    config = dict(base or {})
    for key in ("sort", "preset"):
        if key in data:
            if len(data.getlist(key)) != 1:
                raise ValidationError("Repeated table setting.")
            config[key] = data[key]
    if "page_size" in data:
        try:
            if len(data.getlist("page_size")) != 1:
                raise ValueError
            config["page_size"] = int(data["page_size"])
        except (ValueError, TypeError):
            raise ValidationError("Choose a supported page size.")
    if "columns" in data or "columns_present" in data:
        config["columns"] = data.getlist("columns")
    filters = dict(config.get("filters", {}))
    for key in TABLES[resource]["filters"]:
        if key in data:
            if len(data.getlist(key)) != 1:
                raise ValidationError("Repeated table filter.")
            filters[key] = data[key]
    config["filters"] = filters
    return validate_configuration(resource, config)


def prepare_list(request, queryset, resource):
    try:
        return _prepare_list(request, queryset, resource)
    except ValidationError as exc:
        return settings_error(request, resource, exc.messages), None


def configuration_query(config):
    params = list(config["filters"].items())
    params += [(key, str(config[key])) for key in ("sort", "preset", "page_size")]
    params += [("columns", column) for column in config["columns"]]
    return urlencode(params)


def settings_error(request, resource, errors):
    target = reverse("admin_tools:" + ROUTES[resource])
    try:
        config = configuration_from_query(request.POST if request.method == "POST" else request.GET, resource)
        target += "?" + configuration_query(config)
    except ValidationError:
        pass
    return render(request, "admin_tools/list_settings_error.html", {
        "settings_errors": errors, "table_return_url": target,
        "table_reset_url": reverse("admin_tools:" + ROUTES[resource]),
    }, status=400)


def _prepare_list(request, queryset, resource):
    selected = None
    base = None
    if request.GET.get("view"):
        try:
            selected, base = load_view(actor=request.user, resource=resource, pk=int(request.GET["view"]))
        except (StaffListView.DoesNotExist, ValueError, OverflowError):
            raise Http404("Saved view not found.")
    config = configuration_from_query(request.GET, resource, base)
    if request.GET.get("export") == "csv":
        return export_csv(queryset, resource, config), None
    rows = apply_configuration(queryset, resource, config)
    page = Paginator(rows, config["page_size"]).get_page(request.GET.get("page"))
    spec = TABLES[resource]
    labels = {"created": "Created", "active": "Active", "expiry": "Expiry", "due": "Due date", "assigned": "Assigned to"}
    headings = [{"key": column, "label": labels.get(column, column.replace("_", " ").title())} for column in config["columns"]]
    table_rows = []
    for obj in page.object_list:
        values = row_values(resource, obj)
        table_rows.append({"object": obj, "cells": [{"key": column, "value": values[column]} for column in config["columns"]]})
    from apps.domains.models import Domain
    from apps.billing.models import Invoice
    from apps.support.models import SupportTicket
    status_models = {"domains": Domain, "invoices": Invoice, "tickets": SupportTicket}
    return None, {
        "page_obj": page, "search_q": config["filters"].get("q", ""),
        "status_filter": config["filters"].get("status", ""),
        "list_settings": config, "list_resource": resource,
        "saved_views": StaffListView.objects.filter(owner=request.user, resource=resource)[:20],
        "selected_view": selected, "list_columns": spec["columns"],
        "list_sorts": [{"value": sort, "label": {"created_at": "Created", "expires_at": "Expiry", "due_date": "Due date"}.get(sort.lstrip("-"), sort.lstrip("-").replace("_", " ").title()) + (" — descending" if sort.startswith("-") else " — ascending")} for sort in spec["sorts"]], "list_presets": spec["presets"],
        "list_page_sizes": (10, 25, 50, 100),
        "list_headings": headings, "list_rows": table_rows,
        "list_filter_keys": spec["filters"],
        "list_status_choices": status_models[resource].STATUS_CHOICES if resource in status_models else (),
        "list_reset_url": reverse("admin_tools:" + ROUTES[resource]),
        "list_export_url": "?" + configuration_query(config) + "&export=csv",
    }
