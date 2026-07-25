"""Staff tools for cash/manual domain registration without pre-paid invoice."""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.admin_tools.decorators import staff_member_required
from apps.billing.models import Invoice
from apps.billing.services import LineItemSpec, create_invoice
from apps.domains.models import Domain, DomainContact, DomainOrder, TLDPricing
from apps.domains.tasks import register_domain_order
from apps.products.models import Package
from apps.provisioning.models import WHMPackageSnapshot
from apps.provisioning.tasks import create_provisioning_job
from apps.provisioning.whm_client import WHMClient, WHMClientError
from apps.services.models import Service
from django.utils.text import slugify

logger = logging.getLogger(__name__)

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.(?!-)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$",
    re.I,
)


def _split_domain(domain_name: str) -> tuple[str, str]:
    name = (domain_name or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(name):
        raise ValueError("Enter a valid domain name including the TLD (e.g. example.co.uk).")
    # Prefer multi-part TLDs we know about
    known = list(TLDPricing.objects.filter(is_active=True).values_list("tld", flat=True))
    known = sorted(set(known) | {"co.uk", "org.uk", "me.uk", "com", "uk", "net", "org", "io"}, key=len, reverse=True)
    for tld in known:
        suffix = f".{tld}"
        if name.endswith(suffix) and len(name) > len(suffix):
            return name, tld
    if name.count(".") < 1:
        raise ValueError("Domain must include an extension.")
    label, tld = name.split(".", 1)
    if not label:
        raise ValueError("Invalid domain name.")
    return name, tld


def _ensure_contact(user: User, post) -> DomainContact:
    """Use selected contact, existing default, or create a simple staff-entered contact."""
    contact_id = (post.get("contact_id") or "").strip()
    if contact_id:
        contact = DomainContact.objects.filter(pk=contact_id, user=user).first()
        if contact:
            return contact

    existing = (
        DomainContact.objects.filter(user=user, is_default=True).first()
        or DomainContact.objects.filter(user=user).order_by("id").first()
    )
    if existing and not (post.get("contact_name") or "").strip():
        return existing

    name = (post.get("contact_name") or "").strip() or user.full_name or user.email
    email = (post.get("contact_email") or "").strip() or user.email
    phone = (post.get("contact_phone") or "").strip() or (user.phone or "07000000000")
    phone = re.sub(r"\D", "", phone) or "7000000000"
    address = (post.get("contact_address") or "").strip() or "Address on file"
    city = (post.get("contact_city") or "").strip() or "London"
    postcode = (post.get("contact_postcode") or "").strip() or "SW1A 1AA"
    state = (post.get("contact_state") or "").strip() or city

    profile = getattr(user, "client_profile", None)
    if profile:
        address = address if post.get("contact_address") else (profile.address_line1 or address)
        city = city if post.get("contact_city") else (profile.city or city)
        postcode = postcode if post.get("contact_postcode") else (profile.postcode or postcode)
        state = state if post.get("contact_state") else (profile.county or state)

    contact = DomainContact.objects.create(
        user=user,
        label="Staff registration",
        name=name,
        email=email,
        phone_country_code="44",
        phone=phone[-15:],
        address_line1=address[:255],
        city=city[:100],
        state=state[:100],
        postcode=postcode[:20],
        country="GB",
        is_default=not DomainContact.objects.filter(user=user).exists(),
    )
    return contact


def _price_for_tld(tld: str, years: int) -> Decimal:
    pricing = TLDPricing.objects.filter(tld=tld, is_active=True).first()
    if not pricing:
        return Decimal("0.00")
    return (pricing.registration_price * Decimal(years)).quantize(Decimal("0.01"))


def _extract_whm_pkg_name(row) -> str:
    if isinstance(row, str):
        return row.strip()
    if not isinstance(row, dict):
        return ""
    for key in ("name", "pkg", "package", "pkgname", "plan"):
        val = row.get(key)
        if val:
            return str(val).strip()
    return ""


def _load_whm_hosting_packages() -> tuple[list[dict], str]:
    """Return hosting package options for the dropdown.

    Prefer the last WHM inventory snapshot (safe if WHM is briefly down).
    If that is empty, attempt a live ``listpkgs`` call.
    """
    source = "snapshot"
    names: list[str] = list(
        WHMPackageSnapshot.objects.filter(is_active=True)
        .order_by("name")
        .values_list("name", flat=True)
    )

    # Live refresh when snapshot is empty (or as a soft top-up if WHM responds).
    live_names: list[str] = []
    live_error = ""
    try:
        client = WHMClient()
        for row in client.list_packages() or []:
            name = _extract_whm_pkg_name(row)
            if name:
                live_names.append(name)
    except (WHMClientError, Exception) as exc:
        live_error = str(exc)
        logger.warning("Live WHM package list failed: %s", exc)

    if live_names:
        # Merge live into snapshot set so dropdown matches the server right now.
        merged = sorted(set(names) | set(live_names), key=str.lower)
        names = merged
        source = "whm+snapshot" if names and WHMPackageSnapshot.objects.exists() else "whm"
        # Keep snapshot warm for next time (best-effort, no full sync).
        for name in live_names:
            WHMPackageSnapshot.objects.update_or_create(
                name=name,
                defaults={"is_active": True},
            )
    elif not names and live_error:
        source = f"unavailable ({live_error[:120]})"

    portal_by_whm = {
        (p.whm_package_name or "").strip(): p
        for p in Package.objects.filter(is_active=True).exclude(whm_package_name="")
    }
    options = []
    for name in names:
        portal = portal_by_whm.get(name)
        label = name
        if portal:
            label = f"{name} · portal: {portal.name} (£{portal.price_annually}/yr)"
        else:
            label = f"{name} · WHM package"
        options.append({"name": name, "label": label, "portal_package": portal})
    return options, source


def _resolve_package_for_whm_name(whm_package_name: str) -> Package:
    """Map a WHM package name to a portal Package (create a lightweight row if needed)."""
    name = (whm_package_name or "").strip()
    if not name:
        raise ValueError("WHM package name is required.")

    existing = Package.objects.filter(whm_package_name__iexact=name).order_by("-is_active", "id").first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
            existing.save(update_fields=["is_active", "updated_at"])
        return existing

    # Also match if portal package name equals the WHM plan name.
    by_name = Package.objects.filter(name__iexact=name).order_by("id").first()
    if by_name:
        if not by_name.whm_package_name:
            by_name.whm_package_name = name
            by_name.save(update_fields=["whm_package_name", "updated_at"])
        return by_name

    base_slug = slugify(name)[:40] or "whm-package"
    slug = base_slug
    i = 2
    while Package.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{i}"[:50]
        i += 1

    return Package.objects.create(
        name=name,
        slug=slug,
        description=f"Auto-created from WHM package “{name}” (manual registration tool).",
        price_monthly=Decimal("0.00"),
        price_annually=Decimal("0.00"),
        whm_package_name=name,
        is_active=True,
        show_on_homepage=False,
        is_quotable=False,
    )


@staff_member_required
def manual_domain_register(request):
    """Simple staff form: register domain for a client, optional hosting, no invoice required."""
    users = User.objects.filter(is_active=True).order_by("email")[:500]
    whm_packages, whm_packages_source = _load_whm_hosting_packages()
    contacts_by_user = {}
    for c in DomainContact.objects.select_related("user").order_by("user__email", "label")[:1000]:
        contacts_by_user.setdefault(c.user_id, []).append(c)

    context = {
        "users": users,
        "whm_packages": whm_packages,
        "whm_packages_source": whm_packages_source,
        "contacts_by_user": contacts_by_user,
        "year_choices": list(range(1, 11)),
        "form": {},
    }

    if request.method != "POST":
        return render(request, "admin_tools/manual_domain_register.html", context)

    form = {
        "domain_name": (request.POST.get("domain_name") or "").strip().lower(),
        "user_id": (request.POST.get("user_id") or "").strip(),
        "years": (request.POST.get("years") or "1").strip(),
        "privacy_enabled": request.POST.get("privacy_enabled") == "on",
        "auto_renew": request.POST.get("auto_renew") == "on",
        "create_hosting": request.POST.get("create_hosting") == "on",
        "whm_package_name": (request.POST.get("whm_package_name") or "").strip(),
        "create_invoice": request.POST.get("create_invoice") == "on",
        "invoice_amount": (request.POST.get("invoice_amount") or "").strip(),
        "notes": (request.POST.get("notes") or "").strip(),
        "contact_id": (request.POST.get("contact_id") or "").strip(),
        "contact_name": (request.POST.get("contact_name") or "").strip(),
        "contact_email": (request.POST.get("contact_email") or "").strip(),
        "contact_phone": (request.POST.get("contact_phone") or "").strip(),
        "contact_address": (request.POST.get("contact_address") or "").strip(),
        "contact_city": (request.POST.get("contact_city") or "").strip(),
        "contact_postcode": (request.POST.get("contact_postcode") or "").strip(),
    }
    context["form"] = form

    try:
        years = max(1, min(10, int(form["years"])))
    except ValueError:
        years = 1

    user = User.objects.filter(pk=form["user_id"]).first() if form["user_id"] else None
    if not user:
        messages.error(request, "Select a client account.")
        return render(request, "admin_tools/manual_domain_register.html", context)

    try:
        domain_name, tld = _split_domain(form["domain_name"])
    except ValueError as exc:
        messages.error(request, str(exc))
        return render(request, "admin_tools/manual_domain_register.html", context)

    if Domain.objects.filter(name__iexact=domain_name).exists():
        messages.error(request, f"{domain_name} already exists in the portal.")
        return render(request, "admin_tools/manual_domain_register.html", context)
    blocking = (
        DomainOrder.objects.filter(domain_name__iexact=domain_name)
        .exclude(status=DomainOrder.STATUS_CANCELLED)
        .order_by("-id")
        .first()
    )
    if blocking:
        messages.error(
            request,
            f"An open platform order already exists for {domain_name} "
            f"(order #{blocking.pk}, status: {blocking.get_status_display()}). "
            f"Open Domain orders to process, cancel, or delete it first.",
        )
        context["blocking_order"] = blocking
        return render(request, "admin_tools/manual_domain_register.html", context)

    package = None
    if form["create_hosting"]:
        whm_name = form["whm_package_name"]
        if not whm_name:
            messages.error(request, "Select a WHM hosting package or uncheck Create WHM hosting.")
            return render(request, "admin_tools/manual_domain_register.html", context)
        try:
            package = _resolve_package_for_whm_name(whm_name)
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(request, "admin_tools/manual_domain_register.html", context)

    contact = _ensure_contact(user, request.POST)
    price = _price_for_tld(tld, years)

    order = DomainOrder.objects.create(
        user=user,
        invoice=None,
        domain_name=domain_name,
        tld=tld,
        registration_years=years,
        quoted_price=price,
        total_price=price,
        status=DomainOrder.STATUS_PAID,
        privacy_enabled=form["privacy_enabled"],
        auto_renew=form["auto_renew"],
        dns_provider=Domain.DNS_PROVIDER_CPANEL if form["create_hosting"] else Domain.DNS_PROVIDER_REGISTRAR,
        registration_contact=contact,
        admin_contact=contact,
        tech_contact=contact,
        billing_contact=contact,
        last_error="",
    )

    try:
        # Run registration in-process so staff get immediate success/error.
        domain_id = register_domain_order.apply(args=[order.id]).get(timeout=180)
    except Exception as exc:
        logger.exception("Manual domain registration failed for %s", domain_name)
        order.refresh_from_db()
        messages.error(
            request,
            f"Domain registration failed: {order.last_error or exc}. "
            "Fix the error and retry from the domain order, or check ResellerClub credentials.",
        )
        return redirect("admin_tools:domains_list")

    order.refresh_from_db()
    domain = Domain.objects.filter(pk=domain_id).first() or order.domain
    service = None

    if form["create_hosting"] and package and domain:
        service = Service.objects.create(
            user=user,
            package=package,
            domain_name=domain.name,
            status=Service.STATUS_PENDING,
            billing_period="annually",
            notes=f"Manual staff order {timezone.now():%Y-%m-%d} — cash/offline sale.",
        )
        try:
            create_provisioning_job(service)
            messages.success(request, f"WHM hosting provision queued for {domain.name} ({package.name}).")
        except Exception as exc:
            logger.exception("Hosting provision queue failed")
            messages.warning(request, f"Domain registered, but hosting queue failed: {exc}")

    invoice = None
    if form["create_invoice"] and domain:
        try:
            amount = Decimal(form["invoice_amount"] or str(price))
        except Exception:
            amount = price
        if amount < 0:
            amount = Decimal("0.00")
        lines = [
            LineItemSpec(
                description=f"Domain registration: {domain.name} × {years} year(s) (manual/cash order)",
                unit_price=amount if not (form["create_hosting"] and package) else price,
                quantity=Decimal("1"),
                position=0,
            )
        ]
        if form["create_hosting"] and package:
            lines = [
                LineItemSpec(
                    description=f"Domain registration: {domain.name} × {years} year(s)",
                    unit_price=price,
                    quantity=Decimal("1"),
                    position=0,
                ),
                LineItemSpec(
                    description=f"Hosting: {package.name} ({domain.name})",
                    unit_price=package.price_annually,
                    quantity=Decimal("1"),
                    position=1,
                ),
            ]
            # If staff set a single override total, fold into first line note
            if form["invoice_amount"]:
                try:
                    override = Decimal(form["invoice_amount"])
                    lines = [
                        LineItemSpec(
                            description=(
                                f"Manual order: {domain.name}"
                                + (f" + {package.name}" if package else "")
                                + f" × {years} yr domain"
                            ),
                            unit_price=override,
                            quantity=Decimal("1"),
                            position=0,
                        )
                    ]
                except Exception:
                    pass

        notes = form["notes"] or f"Manual/cash order registered by staff on {timezone.now():%Y-%m-%d %H:%M}."
        invoice = create_invoice(
            user=user,
            line_items=lines,
            source_kind=Invoice.SOURCE_MANUAL_ADMIN,
            status=Invoice.STATUS_UNPAID,
            notes=notes,
            created_by_staff=request.user,
            send_email=False,
        )
        order.invoice = invoice
        order.save(update_fields=["invoice", "updated_at"])
        if service:
            service.invoice = invoice
            service.save(update_fields=["invoice"])

    messages.success(
        request,
        f"Registered {domain.name if domain else domain_name} for {user.email}"
        + (f" · Invoice {invoice.number}" if invoice else " · no invoice created"),
    )
    return redirect(
        reverse("admin_tools:manual_domain_result", args=[order.pk])
    )


@staff_member_required
def manual_domain_result(request, order_id):
    order = get_object_or_404(
        DomainOrder.objects.select_related("user", "domain", "invoice"),
        pk=order_id,
    )
    services = Service.objects.filter(user=order.user, domain_name=order.domain_name).select_related("package")
    whm_packages, whm_packages_source = _load_whm_hosting_packages()
    return render(
        request,
        "admin_tools/manual_domain_result.html",
        {
            "order": order,
            "domain": order.domain,
            "services": services,
            "whm_packages": whm_packages,
            "whm_packages_source": whm_packages_source,
        },
    )


@staff_member_required
@require_POST
def manual_domain_generate_invoice(request, order_id):
    """Create an unpaid invoice after the fact for a manual domain order."""
    order = get_object_or_404(DomainOrder.objects.select_related("user", "domain", "invoice"), pk=order_id)
    if order.invoice_id:
        messages.info(request, f"Order already has invoice {order.invoice.number}.")
        return redirect("admin_tools:invoice_edit", pk=order.invoice_id)

    amount_raw = (request.POST.get("amount") or "").strip()
    include_hosting = request.POST.get("include_hosting") == "on"
    notes = (request.POST.get("notes") or "").strip()

    domain_name = order.domain.name if order.domain_id else order.domain_name
    price = order.total_price or _price_for_tld(order.tld, order.registration_years)
    try:
        if amount_raw:
            price = Decimal(amount_raw)
    except Exception:
        pass

    lines = [
        LineItemSpec(
            description=f"Domain registration: {domain_name} × {order.registration_years} year(s) (manual/cash)",
            unit_price=price,
            quantity=Decimal("1"),
            position=0,
        )
    ]
    service = (
        Service.objects.filter(user=order.user, domain_name=domain_name)
        .select_related("package")
        .order_by("-id")
        .first()
    )
    if include_hosting and service and service.package_id and not amount_raw:
        lines.append(
            LineItemSpec(
                description=f"Hosting: {service.package.name} ({domain_name})",
                unit_price=service.package.price_annually,
                quantity=Decimal("1"),
                position=1,
            )
        )

    invoice = create_invoice(
        user=order.user,
        line_items=lines,
        source_kind=Invoice.SOURCE_MANUAL_ADMIN,
        status=Invoice.STATUS_UNPAID,
        notes=notes or f"Post-registration invoice for manual order of {domain_name}.",
        created_by_staff=request.user,
        send_email=False,
    )
    order.invoice = invoice
    order.save(update_fields=["invoice", "updated_at"])
    if service and not service.invoice_id:
        service.invoice = invoice
        service.save(update_fields=["invoice"])

    messages.success(request, f"Invoice {invoice.number} created. Mark paid when cash is received.")
    return redirect("admin_tools:invoice_edit", pk=invoice.pk)


@staff_member_required
@require_POST
def manual_domain_add_hosting(request, order_id):
    """Attach WHM hosting to an already-registered manual domain order."""
    order = get_object_or_404(DomainOrder.objects.select_related("user", "domain"), pk=order_id)
    domain = order.domain
    if not domain:
        messages.error(request, "Domain is not registered yet — cannot create hosting.")
        return redirect("admin_tools:manual_domain_result", order_id=order.pk)

    whm_name = (request.POST.get("whm_package_name") or "").strip()
    if not whm_name:
        messages.error(request, "Select a WHM hosting package.")
        return redirect("admin_tools:manual_domain_result", order_id=order.pk)
    try:
        package = _resolve_package_for_whm_name(whm_name)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("admin_tools:manual_domain_result", order_id=order.pk)

    if Service.objects.filter(user=order.user, domain_name=domain.name, status=Service.STATUS_ACTIVE).exists():
        messages.warning(request, "An active hosting service already exists for this domain.")
        return redirect("admin_tools:manual_domain_result", order_id=order.pk)

    service = Service.objects.create(
        user=order.user,
        package=package,
        domain_name=domain.name,
        invoice=order.invoice,
        status=Service.STATUS_PENDING,
        billing_period="annually",
        notes=f"Hosting added via manual order tool {timezone.now():%Y-%m-%d}.",
    )
    try:
        create_provisioning_job(service)
        messages.success(request, f"Hosting provision queued ({package.name}).")
    except Exception as exc:
        messages.error(request, f"Failed to queue hosting: {exc}")
    return redirect("admin_tools:manual_domain_result", order_id=order.pk)
