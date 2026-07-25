"""Staff workbench for platform DomainOrder records (pending/failed/etc.)."""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.admin_tools.decorators import staff_member_required
from apps.domains.models import Domain, DomainContact, DomainOrder
from apps.domains.tasks import register_domain_order

logger = logging.getLogger(__name__)


def _orders_qs():
    return DomainOrder.objects.select_related(
        "user",
        "invoice",
        "domain",
        "registration_contact",
    ).order_by("-created_at")


@staff_member_required
def domain_orders_list(request):
    """List domain orders with status filter (default: open platform orders)."""
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "open").strip()

    qs = _orders_qs()
    if status == "open":
        qs = qs.filter(status__in=DomainOrder.OPEN_STATUSES)
    elif status == "all":
        pass
    elif status:
        qs = qs.filter(status=status)

    if q:
        qs = qs.filter(
            Q(domain_name__icontains=q)
            | Q(user__email__icontains=q)
            | Q(registrar_order_id__icontains=q)
            | Q(last_error__icontains=q)
            | Q(invoice__number__icontains=q)
        )

    page_obj = Paginator(qs, 40).get_page(request.GET.get("page"))

    counts = {
        row["status"]: row["c"]
        for row in DomainOrder.objects.values("status").annotate(c=Count("id"))
    }
    open_count = sum(counts.get(s, 0) for s in DomainOrder.OPEN_STATUSES)

    return render(
        request,
        "admin_tools/ops/domain_orders_list.html",
        {
            "page_obj": page_obj,
            "search_q": q,
            "status_filter": status,
            "status_choices": DomainOrder.STATUS_CHOICES,
            "counts": counts,
            "open_count": open_count,
            "total_count": DomainOrder.objects.count(),
        },
    )


@staff_member_required
def domain_order_detail(request, pk):
    order = get_object_or_404(_orders_qs(), pk=pk)
    contacts = DomainContact.objects.filter(user=order.user).order_by("label")
    users = User.objects.filter(is_active=True).order_by("email")[:300]

    if request.method == "POST":
        return _save_order_details(request, order)

    resolved_ns = []
    try:
        from apps.domains.tasks import _build_nameservers

        resolved_ns = _build_nameservers(order)
    except Exception:
        resolved_ns = []

    return render(
        request,
        "admin_tools/ops/domain_order_detail.html",
        {
            "order": order,
            "contacts": contacts,
            "users": users,
            "status_choices": DomainOrder.STATUS_CHOICES,
            "dns_choices": Domain.DNS_CHOICES,
            "resolved_nameservers": resolved_ns,
        },
    )


def _save_order_details(request, order: DomainOrder):
    if order.status == DomainOrder.STATUS_COMPLETED and order.domain_id:
        # Allow limited notes/error clear only
        order.last_error = (request.POST.get("last_error") or "").strip()
        order.save(update_fields=["last_error", "updated_at"])
        messages.success(request, "Completed order updated (error field only).")
        return redirect("admin_tools:domain_order_detail", pk=order.pk)

    domain_name = (request.POST.get("domain_name") or "").strip().lower().rstrip(".")
    if domain_name and domain_name != order.domain_name:
        clash = (
            DomainOrder.objects.filter(domain_name__iexact=domain_name)
            .exclude(pk=order.pk)
            .exclude(status=DomainOrder.STATUS_CANCELLED)
            .exists()
        )
        if clash:
            messages.error(request, f"Another open order already uses {domain_name}.")
            return redirect("admin_tools:domain_order_detail", pk=order.pk)
        if Domain.objects.filter(name__iexact=domain_name).exists():
            messages.error(request, f"Domain {domain_name} already exists in the portal.")
            return redirect("admin_tools:domain_order_detail", pk=order.pk)
        order.domain_name = domain_name
        if "." in domain_name:
            # keep multi-part TLDs simple: use everything after first label
            parts = domain_name.split(".", 1)
            order.tld = parts[1] if len(parts) > 1 else order.tld

    user_id = (request.POST.get("user_id") or "").strip()
    if user_id:
        user = User.objects.filter(pk=user_id).first()
        if user:
            order.user = user

    try:
        years = int(request.POST.get("registration_years") or order.registration_years)
        order.registration_years = max(1, min(10, years))
    except ValueError:
        pass

    for field in ("quoted_price", "total_price"):
        raw = (request.POST.get(field) or "").strip()
        if raw:
            try:
                setattr(order, field, Decimal(raw))
            except (InvalidOperation, ValueError):
                pass

    new_status = (request.POST.get("status") or "").strip()
    valid_statuses = {c[0] for c in DomainOrder.STATUS_CHOICES}
    if new_status in valid_statuses:
        order.status = new_status

    dns = (request.POST.get("dns_provider") or "").strip()
    if dns in {c[0] for c in Domain.DNS_CHOICES}:
        order.dns_provider = dns

    order.privacy_enabled = request.POST.get("privacy_enabled") == "on"
    order.auto_renew = request.POST.get("auto_renew") == "on"
    order.registrar_order_id = (request.POST.get("registrar_order_id") or "").strip()
    order.last_error = (request.POST.get("last_error") or "").strip()

    contact_id = (request.POST.get("registration_contact_id") or "").strip()
    if contact_id:
        contact = DomainContact.objects.filter(pk=contact_id, user=order.user).first()
        if contact:
            order.registration_contact = contact
            order.admin_contact = contact
            order.tech_contact = contact
            order.billing_contact = contact

    order.save()
    messages.success(request, f"Order #{order.pk} ({order.domain_name}) saved.")
    return redirect("admin_tools:domain_order_detail", pk=order.pk)


@staff_member_required
@require_POST
def domain_order_action(request, pk, action):
    order = get_object_or_404(DomainOrder, pk=pk)
    action = (action or "").strip().lower()

    if action == "process":
        return _action_process(request, order)
    if action == "pause":
        return _action_pause(request, order)
    if action == "resume":
        return _action_resume(request, order)
    if action == "cancel":
        return _action_cancel(request, order)
    if action == "delete":
        return _action_delete(request, order)
    if action == "mark_paid":
        return _action_mark_paid(request, order)
    if action == "clear_error":
        order.last_error = ""
        order.save(update_fields=["last_error", "updated_at"])
        messages.success(request, "Error cleared.")
        return redirect("admin_tools:domain_order_detail", pk=order.pk)
    if action == "retry_failed":
        if order.status != DomainOrder.STATUS_FAILED:
            messages.error(request, "Only failed orders can be retried this way.")
            return redirect("admin_tools:domain_order_detail", pk=order.pk)
        order.status = DomainOrder.STATUS_PAID
        order.last_error = ""
        order.save(update_fields=["status", "last_error", "updated_at"])
        return _action_process(request, order)

    messages.error(request, f"Unknown action: {action}")
    return redirect("admin_tools:domain_order_detail", pk=order.pk)


def _action_process(request, order: DomainOrder):
    if order.status == DomainOrder.STATUS_COMPLETED and order.domain_id:
        messages.info(request, "Order already completed.")
        return redirect("admin_tools:domain_order_detail", pk=order.pk)
    if order.status == DomainOrder.STATUS_CANCELLED:
        messages.error(request, "Cancelled orders cannot be processed. Resume/recreate first.")
        return redirect("admin_tools:domain_order_detail", pk=order.pk)
    if order.status == DomainOrder.STATUS_PAUSED:
        messages.error(request, "Order is paused — resume it before processing.")
        return redirect("admin_tools:domain_order_detail", pk=order.pk)
    if order.status == DomainOrder.STATUS_PENDING_PAYMENT and (
        not order.invoice_id or order.invoice.status != order.invoice.STATUS_PAID
    ):
        # Staff override: mark paid path for cash
        if request.POST.get("force") != "1":
            messages.error(
                request,
                "Order is pending payment. Mark paid first, or submit process with force=1 after cash received.",
            )
            return redirect("admin_tools:domain_order_detail", pk=order.pk)
        order.status = DomainOrder.STATUS_PAID
        order.save(update_fields=["status", "updated_at"])

    if order.status in (DomainOrder.STATUS_DRAFT, DomainOrder.STATUS_FAILED):
        order.status = DomainOrder.STATUS_PAID
        order.last_error = ""
        order.save(update_fields=["status", "last_error", "updated_at"])

    try:
        domain_id = register_domain_order.apply(args=[order.id]).get(timeout=180)
        order.refresh_from_db()
        messages.success(
            request,
            f"Processed {order.domain_name} → status {order.get_status_display()}"
            + (f" · domain id {domain_id}" if domain_id else ""),
        )
    except Exception as exc:
        order.refresh_from_db()
        logger.exception("Process domain order %s failed", order.pk)
        messages.error(request, f"Process failed: {order.last_error or exc}")
    return redirect("admin_tools:domain_order_detail", pk=order.pk)


def _action_pause(request, order: DomainOrder):
    if order.status in (DomainOrder.STATUS_COMPLETED, DomainOrder.STATUS_CANCELLED):
        messages.error(request, "Cannot pause a completed or cancelled order.")
        return redirect("admin_tools:domain_order_detail", pk=order.pk)
    order.status = DomainOrder.STATUS_PAUSED
    note = (request.POST.get("reason") or "").strip()
    if note:
        order.last_error = f"[PAUSED] {note}"
    order.save(update_fields=["status", "last_error", "updated_at"])
    messages.success(request, f"Order {order.domain_name} paused.")
    return redirect("admin_tools:domain_order_detail", pk=order.pk)


def _action_resume(request, order: DomainOrder):
    if order.status != DomainOrder.STATUS_PAUSED:
        messages.error(request, "Only paused orders can be resumed.")
        return redirect("admin_tools:domain_order_detail", pk=order.pk)
    # Resume to paid if no invoice, else pending_payment / paid based on invoice
    if order.invoice_id and order.invoice.status != order.invoice.STATUS_PAID:
        order.status = DomainOrder.STATUS_PENDING_PAYMENT
    else:
        order.status = DomainOrder.STATUS_PAID
    if order.last_error.startswith("[PAUSED]"):
        order.last_error = ""
    order.save(update_fields=["status", "last_error", "updated_at"])
    messages.success(request, f"Order {order.domain_name} resumed ({order.get_status_display()}).")
    return redirect("admin_tools:domain_order_detail", pk=order.pk)


def _action_cancel(request, order: DomainOrder):
    if order.status == DomainOrder.STATUS_COMPLETED and order.domain_id:
        messages.error(request, "Cannot cancel a completed registration that already created a domain.")
        return redirect("admin_tools:domain_order_detail", pk=order.pk)
    order.status = DomainOrder.STATUS_CANCELLED
    reason = (request.POST.get("reason") or "").strip()
    if reason:
        order.last_error = f"[CANCELLED] {reason}"
    order.save(update_fields=["status", "last_error", "updated_at"])
    messages.success(
        request,
        f"Order {order.domain_name} cancelled. Domain name is free for a new platform order.",
    )
    return redirect("admin_tools:domain_orders_list")


def _action_delete(request, order: DomainOrder):
    if order.status == DomainOrder.STATUS_COMPLETED and order.domain_id:
        messages.error(
            request,
            "Cannot delete a completed order linked to a domain. Cancel is blocked too — edit the domain record instead.",
        )
        return redirect("admin_tools:domain_order_detail", pk=order.pk)
    name = order.domain_name
    pk = order.pk
    # Unlink domain if any partial link without completed status
    if order.domain_id and order.status != DomainOrder.STATUS_COMPLETED:
        order.domain = None
        order.save(update_fields=["domain", "updated_at"])
    order.delete()
    messages.success(request, f"Deleted platform order #{pk} for {name}.")
    return redirect("admin_tools:domain_orders_list")


def _action_mark_paid(request, order: DomainOrder):
    if order.status == DomainOrder.STATUS_COMPLETED:
        messages.info(request, "Order already completed.")
        return redirect("admin_tools:domain_order_detail", pk=order.pk)
    order.status = DomainOrder.STATUS_PAID
    if order.last_error.startswith("[PAUSED]") or order.last_error.startswith("[CANCELLED]"):
        order.last_error = ""
    order.save(update_fields=["status", "last_error", "updated_at"])
    if order.invoice_id and order.invoice.status != order.invoice.STATUS_PAID:
        messages.warning(
            request,
            f"Order marked paid on platform, but invoice {order.invoice.number} is still "
            f"{order.invoice.get_status_display()}. Mark the invoice paid separately if cash is received.",
        )
    else:
        messages.success(request, f"Order {order.domain_name} marked paid (ready to process).")
    return redirect("admin_tools:domain_order_detail", pk=order.pk)
