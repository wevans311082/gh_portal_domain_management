import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.domains.models import Domain
from .forms import DNSRecordForm
from .models import DNSZone, DNSRecord
from .sync import ensure_zone, import_records, push_record

logger = logging.getLogger(__name__)


def _sync_record_to_cloudflare(zone, record, action="create"):
    """Push a single record to Cloudflare. Silently logs on failure."""
    if zone.provider != "cloudflare":
        return

    try:
        from apps.cloudflare_integration.services import CloudflareService
        cf = CloudflareService()
        zone_cf_id = zone.domain.cloudflare_zone_id  # stored on Domain model

        if action == "create":
            result = cf.create_dns_record(
                zone_cf_id,
                record_type=record.record_type,
                name=record.name,
                content=record.content,
                ttl=record.ttl,
                proxied=record.proxied,
            )
            record.external_id = result.get("result", {}).get("id", "")
            record.save(update_fields=["external_id"])

        elif action == "update" and record.external_id:
            cf.update_dns_record(
                zone_cf_id,
                record.external_id,
                type=record.record_type,
                name=record.name,
                content=record.content,
                ttl=record.ttl,
                proxied=record.proxied,
            )

        elif action == "delete" and record.external_id:
            cf.delete_dns_record(zone_cf_id, record.external_id)

    except Exception as exc:
        logger.error("Cloudflare DNS sync failed (action=%s, record=%s): %s", action, record.pk, exc)


@login_required
def zone_detail(request, domain_pk):
    domain = get_object_or_404(Domain, pk=domain_pk, user=request.user)
    if request.method == "POST" and request.POST.get("action") == "ensure_zone":
        zone = ensure_zone(domain)
        imported = import_records(zone)
        messages.success(request, f"DNS zone ready. Imported {imported} record(s) from {zone.provider}.")
        return redirect("dns:zone_detail", domain_pk=domain.pk)
    if request.method == "POST" and request.POST.get("action") == "refresh":
        zone = ensure_zone(domain)
        imported = import_records(zone)
        messages.success(request, f"Refreshed zone from {zone.provider}. {imported} new record(s).")
        return redirect("dns:zone_detail", domain_pk=domain.pk)
    zone = getattr(domain, "dns_zone", None)
    records = zone.records.filter(is_active=True).order_by("record_type", "name") if zone else []
    return render(request, "dns/zone_detail.html", {
        "domain": domain,
        "zone": zone,
        "records": records,
    })


@login_required
def record_add(request, domain_pk):
    domain = get_object_or_404(Domain, pk=domain_pk, user=request.user)
    zone = ensure_zone(domain)

    if request.method == "POST":
        form = DNSRecordForm(request.POST)
        if form.is_valid():
            record = form.save(commit=False)
            record.zone = zone
            record.save()
            try:
                push_record(zone, record, action="create")
            except Exception as exc:
                messages.warning(request, f"Record saved locally but provider sync failed: {exc}")
            else:
                messages.success(request, f"{record.record_type} record added successfully.")
            return redirect("dns:zone_detail", domain_pk=domain_pk)
    else:
        form = DNSRecordForm()

    return render(request, "dns/record_form.html", {
        "domain": domain,
        "zone": zone,
        "form": form,
        "action": "Add",
    })


@login_required
def record_edit(request, domain_pk, record_pk):
    domain = get_object_or_404(Domain, pk=domain_pk, user=request.user)
    zone = get_object_or_404(DNSZone, domain=domain)
    record = get_object_or_404(DNSRecord, pk=record_pk, zone=zone)

    if request.method == "POST":
        form = DNSRecordForm(request.POST, instance=record)
        if form.is_valid():
            record = form.save()
            try:
                push_record(zone, record, action="update")
            except Exception as exc:
                messages.warning(request, f"Record saved locally but provider sync failed: {exc}")
            else:
                messages.success(request, f"{record.record_type} record updated successfully.")
            return redirect("dns:zone_detail", domain_pk=domain_pk)
    else:
        form = DNSRecordForm(instance=record)

    return render(request, "dns/record_form.html", {
        "domain": domain,
        "zone": zone,
        "form": form,
        "record": record,
        "action": "Edit",
    })


@login_required
@require_POST
def record_delete(request, domain_pk, record_pk):
    domain = get_object_or_404(Domain, pk=domain_pk, user=request.user)
    zone = get_object_or_404(DNSZone, domain=domain)
    record = get_object_or_404(DNSRecord, pk=record_pk, zone=zone)

    try:
        push_record(zone, record, action="delete")
    except Exception as exc:
        messages.warning(request, f"Deleted locally but provider sync failed: {exc}")
    record.is_active = False
    record.save(update_fields=["is_active"])
    messages.success(request, f"{record.record_type} record deleted.")
    return redirect("dns:zone_detail", domain_pk=domain_pk)

