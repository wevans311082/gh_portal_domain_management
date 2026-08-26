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
from apps.provisioning.whm_client import WHMClient, WHMClientError
from apps.provisioning.whm_sync import WHMSyncService
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
    value = (raw or "").strip().lower().replace(" ", "")
    mapping = {
        "active": Domain.STATUS_ACTIVE,
        "inactive": Domain.STATUS_EXPIRED,
        "expired": Domain.STATUS_EXPIRED,
        "suspended": Domain.STATUS_SUSPENDED,
        "pendingdeleterestorable": Domain.STATUS_EXPIRED,
        "pendingdelete": Domain.STATUS_EXPIRED,
        "deleted": Domain.STATUS_TRANSFERRED,
        "transferredaway": Domain.STATUS_TRANSFERRED,
        "archived": Domain.STATUS_CANCELLED,
    }
    return mapping.get(value, Domain.STATUS_PENDING)


def _normalize_whm_account(acct) -> dict:
    if acct is None:
        return {}
    if isinstance(acct, WHMAccountSnapshot):
        return {
            "username": acct.username,
            "domain": (acct.domain or "").lower(),
            "email": acct.email or "",
            "plan": acct.plan or "",
            "suspended": bool(acct.suspended),
            "source": "snapshot",
        }
    if isinstance(acct, dict):
        domain = str(acct.get("domain") or acct.get("Domain") or "").strip().lower()
        return {
            "username": str(acct.get("user") or acct.get("username") or "").strip(),
            "domain": domain,
            "email": str(acct.get("email") or "").strip(),
            "plan": str(acct.get("plan") or acct.get("pkg") or "").strip(),
            "suspended": str(acct.get("suspended") or "0") in {"1", "true", "True"},
            "diskused": acct.get("diskused") or acct.get("disk_used") or "",
            "disklimit": acct.get("disklimit") or acct.get("disk_limit") or "",
            "source": "live",
        }
    return {}


def _whm_accounts_by_domain() -> dict[str, dict]:
    by_domain: dict[str, dict] = {}
    for snap in WHMAccountSnapshot.objects.filter(is_active=True):
        row = _normalize_whm_account(snap)
        if row.get("domain"):
            by_domain[row["domain"]] = row
        if row.get("username"):
            by_domain.setdefault(f"user:{row['username'].lower()}", row)
    try:
        for acct in WHMClient().list_accounts():
            row = _normalize_whm_account(acct)
            if row.get("domain"):
                by_domain[row["domain"]] = row
            if row.get("username"):
                by_domain[f"user:{row['username'].lower()}"] = row
    except Exception:
        pass
    return by_domain


def _build_rows(orders: list[dict], customers: list[dict]) -> list[dict]:
    local_domains = {
        d.name.lower(): d
        for d in Domain.objects.select_related("user").all()
    }
    services = list(Service.objects.select_related("user", "package").exclude(domain_name=""))
    services_by_domain = {}
    for svc in services:
        services_by_domain.setdefault((svc.domain_name or "").lower(), []).append(svc)
        if svc.cpanel_domain:
            services_by_domain.setdefault(svc.cpanel_domain.lower(), []).append(svc)
    whm_index = _whm_accounts_by_domain()
    customers_by_id = {}
    customers_by_email = {}
    for cust in customers:
        cid = str(cust.get("customerid") or cust.get("customer-id") or cust.get("id") or "").strip()
        email = str(cust.get("username") or cust.get("emailaddr") or cust.get("email") or "").strip().lower()
        if cid:
            customers_by_id[cid] = cust
        if email:
            customers_by_email[email] = cust

    rows = []
    for order in orders:
        domain_name = (order.get("domainname") or order.get("domain") or "").strip().lower()
        order_id = str(order.get("orderid") or "").strip()
        status = order.get("currentstatus") or ""
        customer_id = str(order.get("customerid") or order.get("customer-id") or "").strip()
        local = local_domains.get(domain_name)
        hosting = services_by_domain.get(domain_name, [])
        whm = whm_index.get(domain_name) or {}
        if not whm and hosting:
            username = (hosting[0].cpanel_username or "").lower()
            if username:
                whm = whm_index.get(f"user:{username}") or {}
        customer = customers_by_id.get(customer_id) or {}
        if not customer and whm.get("email"):
            customer = customers_by_email.get(whm["email"].lower()) or {}
        nameservers = order.get("nameservers") or [
            ns for ns in (order.get("ns1"), order.get("ns2"), order.get("ns3"), order.get("ns4")) if ns
        ]
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
                or whm.get("email")
                or "",
                "customer_name": (customer or {}).get("name") or (customer or {}).get("company") or "",
                "nameservers": nameservers,
                "local_domain": local,
                "hosting_services": hosting,
                "whm_account": whm,
                "in_portal": bool(local),
                "in_whm": bool(whm) or bool(hosting),
                "raw": order,
            }
        )
    rows.sort(key=lambda row: row.get("domain_name") or "")
    return rows


def _load_orders_and_customers():
    client = ResellerClubClient()
    client.ensure_configured()
    orders = client.list_all_domain_orders(
        no_of_records=50,
        status="All",
        include_details=False,
        max_pages=80,
    )
    customers = []
    try:
        customers = client.list_customers(no_of_records=50)
    except ResellerClubError:
        customers = []
    return client, orders, customers


@staff_member_required
def resellerclub_hub(request):
    error = ""
    orders = []
    customers = []
    try:
        _client, orders, customers = _load_orders_and_customers()
    except Exception as exc:
        error = str(exc)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "refresh":
            messages.success(request, f"Pulled {len(orders)} ResellerClub domain orders.")
            return redirect("admin_tools:resellerclub_hub")
        if action == "sync_all":
            return _sync_inventory(request, orders)
        if action == "import_order":
            return _import_order(request, orders)
        if action == "sync_order":
            return _sync_order(request, orders)
        if action == "link_user":
            return _link_user(request)

    query = (request.GET.get("q") or "").strip().lower()
    rows = _build_rows(orders, customers)
    if query:
        rows = [
            row
            for row in rows
            if query in (row.get("domain_name") or "")
            or query in (row.get("order_id") or "")
            or query in (row.get("customer_email") or "").lower()
            or query in (row.get("status") or "").lower()
        ]
    linked = sum(1 for row in rows if row["in_portal"])
    unmatched = sum(1 for row in rows if not row["in_portal"])
    whm_linked = sum(1 for row in rows if row["in_whm"])
    return render(
        request,
        "admin_tools/resellerclub.html",
        {
            "rows": rows,
            "error": error,
            "query": query,
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
        if order_id and str(order.get("orderid") or "") == order_id:
            return order
        if domain_name and (order.get("domainname") or "").lower() == domain_name.lower():
            return order
    return None


def _enrich_order(order: dict, order_id: str, domain_name: str) -> dict:
    client = ResellerClubClient()
    details = {}
    try:
        if order_id:
            details = client.get_order_details(order_id) or {}
        elif domain_name:
            details = client.get_order_details_by_name(domain_name) or {}
    except ResellerClubError:
        details = {}
    merged = {**(order or {}), **(details or {})}
    return client._normalize_order_record(merged) if merged else {}


def _resolve_user(order: dict, domain_name: str, explicit_user_id=None) -> User | None:
    if explicit_user_id:
        return get_object_or_404(User, pk=explicit_user_id)
    existing = Domain.objects.filter(name__iexact=domain_name).select_related("user").first()
    if existing:
        return existing.user
    service = (
        Service.objects.filter(domain_name__iexact=domain_name)
        .select_related("user")
        .first()
    )
    if service:
        return service.user
    email = (
        (order.get("email") or order.get("username") or order.get("customeremail") or "")
        .strip()
        .lower()
    )
    if email:
        return User.objects.filter(email__iexact=email).first()
    whm = _whm_accounts_by_domain().get(domain_name.lower()) or {}
    if whm.get("email"):
        return User.objects.filter(email__iexact=whm["email"]).first()
    return None


def _upsert_domain(order: dict, user: User) -> tuple[Domain, bool]:
    domain_name = (order.get("domainname") or order.get("domain") or "").strip().lower()
    order_id = str(order.get("orderid") or "").strip()
    tld = domain_name.split(".", 1)[1] if "." in domain_name else ""
    expires = _parse_epoch_or_iso(order.get("expiry_date") or order.get("endtime"))
    registered = _parse_epoch_or_iso(order.get("creation_date") or order.get("creationtime"))
    domain, created = Domain.objects.get_or_create(
        name=domain_name,
        defaults={
            "user": user,
            "tld": tld,
            "status": _status_from_rc(order.get("currentstatus") or ""),
            "registrar_id": order_id,
            "expires_at": expires,
            "registered_at": registered,
        },
    )
    if not created:
        domain.user = user
        if order_id:
            domain.registrar_id = order_id
        domain.status = _status_from_rc(order.get("currentstatus") or domain.status)
        if expires:
            domain.expires_at = expires
        if registered and not domain.registered_at:
            domain.registered_at = registered
        domain.save()
    _apply_nameservers_from_order(domain, order)
    domain.save()
    return domain, created


def _import_order(request, orders):
    order_id = (request.POST.get("order_id") or "").strip()
    domain_name = (request.POST.get("domain_name") or "").strip().lower()
    user = _resolve_user({}, domain_name, request.POST.get("user_id"))
    if not user:
        messages.error(request, "Select a portal user to import this domain against.")
        return redirect("admin_tools:resellerclub_hub")
    order = _find_order(orders, order_id, domain_name) or {}
    order = _enrich_order(order, order_id, domain_name)
    if not domain_name:
        domain_name = (order.get("domainname") or "").strip().lower()
    if not domain_name or "." not in domain_name:
        messages.error(request, "Could not determine the domain name for this order.")
        return redirect("admin_tools:resellerclub_hub")
    order.setdefault("domainname", domain_name)
    domain, created = _upsert_domain(order, user)
    messages.success(request, f"{'Imported' if created else 'Updated'} {domain.name} for {user.email}.")
    return redirect("admin_tools:resellerclub_hub")


def _sync_order(request, orders):
    domain_id = request.POST.get("domain_id")
    domain = get_object_or_404(Domain, pk=domain_id)
    order_id = domain.registrar_id or (request.POST.get("order_id") or "").strip()
    order = _find_order(orders, order_id, domain.name) or {}
    try:
        order = _enrich_order(order, order_id, domain.name)
    except Exception as exc:
        messages.error(request, f"ResellerClub sync failed: {exc}")
        return redirect("admin_tools:resellerclub_hub")
    if not order:
        messages.error(request, f"No ResellerClub record found for {domain.name}.")
        return redirect("admin_tools:resellerclub_hub")
    _upsert_domain(order, domain.user)
    messages.success(request, f"Synced {domain.name} from ResellerClub.")
    return redirect("admin_tools:resellerclub_hub")


def _sync_inventory(request, orders):
    created = 0
    updated = 0
    skipped = 0
    whm_message = ""
    try:
        result = WHMSyncService().sync_all()
        account_count = result.get("account_count") if isinstance(result, dict) else None
        whm_message = (
            f"WHM snapshot refresh completed ({account_count} accounts)."
            if account_count is not None
            else "WHM snapshot refresh completed."
        )
    except Exception as exc:
        whm_message = f"WHM refresh warning: {exc}"

    for order in orders:
        domain_name = (order.get("domainname") or "").strip().lower()
        if not domain_name or "." not in domain_name:
            skipped += 1
            continue
        user = _resolve_user(order, domain_name)
        if not user:
            skipped += 1
            continue
        try:
            payload = order
            if not order.get("expiry_date") or not (order.get("ns1") or order.get("nameservers")):
                payload = _enrich_order(order, str(order.get("orderid") or ""), domain_name) or order
            _domain, was_created = _upsert_domain(payload, user)
        except Exception:
            skipped += 1
            continue
        if was_created:
            created += 1
        else:
            updated += 1

    messages.success(
        request,
        f"{whm_message} Registrar sync: {created} imported, {updated} updated, {skipped} unmatched (need a portal user).",
    )
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
    ns_values = list(order.get("nameservers") or [])
    for key in ("ns1", "ns2", "ns3", "ns4", "nameserver1", "nameserver2"):
        val = order.get(key)
        if isinstance(val, str) and val.strip():
            ns_values.append(val.strip().lower())
    ns_list = order.get("ns") or order.get("nserver")
    if isinstance(ns_list, list):
        for item in ns_list:
            host = item.get("ns") if isinstance(item, dict) else str(item or "")
            if host:
                ns_values.append(host.strip().lower())
    seen = []
    for host in ns_values:
        host = str(host or "").strip().lower()
        if host and host not in seen:
            seen.append(host)
    if seen:
        domain.nameserver1 = seen[0] if len(seen) > 0 else ""
        domain.nameserver2 = seen[1] if len(seen) > 1 else ""
        domain.nameserver3 = seen[2] if len(seen) > 2 else ""
        domain.nameserver4 = seen[3] if len(seen) > 3 else ""
        domain.save(update_fields=["nameserver1", "nameserver2", "nameserver3", "nameserver4", "updated_at"])
