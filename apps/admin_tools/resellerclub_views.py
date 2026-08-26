"""Admin hub for ResellerClub inventory, linking, and WHM/portal sync."""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.accounts.models import User
from apps.domains.models import Domain
from apps.domains.resellerclub_client import ResellerClubClient, ResellerClubError
from apps.provisioning.models import WHMAccountSnapshot
from apps.services.models import Service
from .decorators import staff_member_required


def _parse_epoch_or_iso(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    parsed = parse_date(text[:10]) if text else None
    if parsed:
        return parsed
    try:
        epoch = int(text)
        if epoch > 0:
            return datetime.fromtimestamp(epoch, tz=dt_timezone.utc).date()
    except (TypeError, ValueError):
        return None
    return None


def _status_from_rc(raw: str) -> str:
    value = (raw or "").strip().lower()
    mapping = {
        "active": Domain.STATUS_ACTIVE,
        "inactive": Domain.STATUS_EXPIRED,
        "expired": Domain.STATUS_EXPIRED,
        "suspended": Domain.STATUS_SUSPENDED,
        "pendingdelete": Domain.STATUS_EXPIRED,
        "deleted": Domain.STATUS_TRANSFERRED,
        "transferredaway": Domain.STATUS_TRANSFERRED,
    }
    return mapping.get(value.replace(" ", ""), Domain.STATUS_PENDING)


def _build_rows(orders: list[dict], customers: list[dict]) -> list[dict]:
    local_domains = {
        d.name.lower(): d
        for d in Domain.objects.select_related("user").all()
    }
    services = list(Service.objects.select_related("user", "package").exclude(domain_name=""))
    services_by_domain = {}
    for svc in services:
        services_by_domain.setdefault(svc.domain_name.lower(), []).append(svc)
    whm_by_domain = {
        (snap.domain or "").lower(): snap
        for snap in WHMAccountSnapshot.objects.all()
        if snap.domain
    }
    customers_by_id = {}
    for cust in customers:
        cid = str(cust.get("customerid") or cust.get("customer-id") or cust.get("id") or "").strip()
        if cid:
            customers_by_id[cid] = cust

    rows = []
    for order in orders:
        domain_name = (order.get("domainname") or order.get("domain") or "").strip().lower()
        order_id = str(order.get("orderid") or "").strip()
        status = order.get("currentstatus") or ""
        customer_id = str(
            order.get("customerid") or order.get("customer-id") or order.get("entityid") or ""
        ).strip()
        local = local_domains.get(domain_name)
        hosting = services_by_domain.get(domain_name, [])
        whm = whm_by_domain.get(domain_name)
        customer = customers_by_id.get(customer_id)
        rows.append(
            {
                "domain_name": domain_name,
                "order_id": order_id,
                "status": status,
                "expiry": order.get("expiry_date") or "",
                "created": order.get("creation_date") or "",
                "customer_id": customer_id,
                "customer_email": (customer or {}).get("username")
                or (customer or {}).get("emailaddr")
                or (customer or {}).get("email")
                or "",
                "customer_name": (customer or {}).get("name") or (customer or {}).get("company") or "",
                "local_domain": local,
                "hosting_services": hosting,
                "whm_account": whm,
                "in_portal": bool(local),
                "in_whm": bool(whm) or bool(hosting),
                "raw": order,
            }
        )
    return rows


@staff_member_required
def resellerclub_hub(request):
    error = ""
    orders = []
    customers = []
    try:
        client = ResellerClubClient()
        client.ensure_configured()
        orders = client.list_all_domain_orders(no_of_records=100, status="All", max_pages=10)
        try:
            customers = client.list_customers(no_of_records=50)
        except ResellerClubError:
            customers = []
    except Exception as exc:
        error = str(exc)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "refresh":
            messages.success(request, f"Pulled {len(orders)} ResellerClub domain orders.")
            return redirect("admin_tools:resellerclub_hub")
        if action == "import_order":
            return _import_order(request, orders)
        if action == "sync_order":
            return _sync_order(request, orders)
        if action == "link_user":
            return _link_user(request)

    rows = _build_rows(orders, customers)
    linked = sum(1 for row in rows if row["in_portal"])
    unmatched = sum(1 for row in rows if not row["in_portal"])
    whm_linked = sum(1 for row in rows if row["in_whm"])
    return render(
        request,
        "admin_tools/resellerclub.html",
        {
            "rows": rows,
            "error": error,
            "order_count": len(rows),
            "linked_count": linked,
            "unmatched_count": unmatched,
            "whm_linked_count": whm_linked,
            "customer_count": len(customers),
            "users": User.objects.order_by("email")[:400],
        },
    )


def _find_order(orders, order_id: str, domain_name: str) -> dict | None:
    for order in orders:
        if str(order.get("orderid") or "") == order_id:
            return order
        if (order.get("domainname") or "").lower() == domain_name.lower():
            return order
    return None


def _import_order(request, orders):
    order_id = (request.POST.get("order_id") or "").strip()
    domain_name = (request.POST.get("domain_name") or "").strip().lower()
    user_id = request.POST.get("user_id")
    user = get_object_or_404(User, pk=user_id) if user_id else None
    if not user:
        messages.error(request, "Select a portal user to import this domain against.")
        return redirect("admin_tools:resellerclub_hub")
    order = _find_order(orders, order_id, domain_name)
    if not order:
        try:
            order = ResellerClubClient().get_order_details(order_id) if order_id else {}
        except ResellerClubError:
            order = {}
    if not domain_name:
        domain_name = (order.get("domainname") or order.get("domain") or "").strip().lower()
    if not domain_name or "." not in domain_name:
        messages.error(request, "Could not determine the domain name for this order.")
        return redirect("admin_tools:resellerclub_hub")
    tld = domain_name.split(".", 1)[1]
    expires = _parse_epoch_or_iso(order.get("expiry_date") or order.get("endtime"))
    registered = _parse_epoch_or_iso(order.get("creation_date") or order.get("creationtime"))
    domain, created = Domain.objects.get_or_create(
        name=domain_name,
        defaults={
            "user": user,
            "tld": tld,
            "status": _status_from_rc(order.get("currentstatus") or ""),
            "registrar_id": str(order.get("orderid") or order_id),
            "expires_at": expires,
            "registered_at": registered,
        },
    )
    if not created:
        domain.user = user
        domain.registrar_id = str(order.get("orderid") or order_id or domain.registrar_id)
        domain.status = _status_from_rc(order.get("currentstatus") or domain.status)
        if expires:
            domain.expires_at = expires
        domain.save()
    _apply_nameservers_from_order(domain, order)
    messages.success(request, f"{'Imported' if created else 'Updated'} {domain.name} for {user.email}.")
    return redirect("admin_tools:resellerclub_hub")


def _sync_order(request, orders):
    domain_id = request.POST.get("domain_id")
    domain = get_object_or_404(Domain, pk=domain_id)
    order_id = domain.registrar_id or (request.POST.get("order_id") or "").strip()
    try:
        client = ResellerClubClient()
        details = client.get_order_details(order_id) if order_id else {}
    except ResellerClubError as exc:
        messages.error(request, f"ResellerClub sync failed: {exc}")
        return redirect("admin_tools:resellerclub_hub")
    if details:
        status = details.get("currentstatus") or details.get("orderstatus") or ""
        if status:
            domain.status = _status_from_rc(status)
        expires = _parse_epoch_or_iso(details.get("endtime") or details.get("expiry_date"))
        if expires:
            domain.expires_at = expires
        if order_id:
            domain.registrar_id = str(order_id)
        _apply_nameservers_from_order(domain, details)
        domain.save()
    messages.success(request, f"Synced {domain.name} from ResellerClub.")
    return redirect("admin_tools:resellerclub_hub")


def _link_user(request):
    domain_id = request.POST.get("domain_id")
    user_id = request.POST.get("user_id")
    domain = get_object_or_404(Domain, pk=domain_id)
    user = get_object_or_404(User, pk=user_id)
    domain.user = user
    domain.save(update_fields=["user", "updated_at"])
    service_id = request.POST.get("service_id")
    if service_id:
        service = get_object_or_404(Service, pk=service_id)
        service.user = user
        if not service.domain_name:
            service.domain_name = domain.name
        service.save(update_fields=["user", "domain_name", "updated_at"])
    messages.success(request, f"Linked {domain.name} to {user.email}.")
    return redirect("admin_tools:resellerclub_hub")


def _apply_nameservers_from_order(domain: Domain, order: dict):
    ns_values = []
    for key in ("ns1", "ns2", "ns3", "ns4", "nameserver1", "nameserver2"):
        val = order.get(key)
        if isinstance(val, str) and val.strip():
            ns_values.append(val.strip().lower())
    ns_list = order.get("ns") or order.get("nameservers") or order.get("nserver")
    if isinstance(ns_list, list):
        for item in ns_list:
            host = item.get("ns") if isinstance(item, dict) else str(item or "")
            if host:
                ns_values.append(host.strip().lower())
    # de-dupe preserving order
    seen = []
    for host in ns_values:
        if host and host not in seen:
            seen.append(host)
    if seen:
        domain.nameserver1 = seen[0] if len(seen) > 0 else ""
        domain.nameserver2 = seen[1] if len(seen) > 1 else ""
        domain.nameserver3 = seen[2] if len(seen) > 2 else ""
        domain.nameserver4 = seen[3] if len(seen) > 3 else ""
