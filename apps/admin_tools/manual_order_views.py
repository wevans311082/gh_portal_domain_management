"""Staff tools for cash/manual domain registration without pre-paid invoice."""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.core.exceptions import ValidationError
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


def _ensure_contact(user: User, post, tld="") -> DomainContact:
    """Reuse an owned, complete contact or validate real staff-entered details."""
    from django.core.exceptions import ValidationError
    from apps.domains.forms import DomainContactForm
    from apps.domains.registrar_policy import policy_for
    policy_for(tld)

    selected = str(post.get("contact_id") or "").strip()
    existing = None
    if selected:
        if len(selected) > 19 or not selected.isascii() or not selected.isdigit() or int(selected) > 2**63 - 1:
            raise ValidationError("Select a valid contact belonging to this client.")
        existing = DomainContact.objects.filter(pk=selected, user=user).first()
        if existing is None:
            raise ValidationError("Select a valid contact belonging to this client.")
    else:
        existing = (
            DomainContact.objects.filter(user=user, is_default=True).first()
            or DomainContact.objects.filter(user=user).order_by("id").first()
        )

    aliases = {
        "name": "contact_name", "email": "contact_email", "phone": "contact_phone",
        "phone_country_code": "contact_phone_country_code",
        "address_line1": "contact_address", "address_line2": "contact_address_line2",
        "city": "contact_city", "state": "contact_state", "postcode": "contact_postcode",
        "country": "contact_country", "registrant_type": "contact_registrant_type",
        "company": "contact_company", "company_number": "contact_company_number",
        "us_nexus_category": "contact_us_nexus_category", "us_application_purpose": "contact_us_application_purpose",
        "us_nexus_certified": "contact_us_nexus_certified",
    }
    supplied = {field: str(post.get(alias) or "").strip() for field, alias in aliases.items()}
    if existing and not any(supplied.values()):
        existing.as_resellerclub_payload("1", tld)
        return existing

    profile = getattr(user, "client_profile", None)
    values = {field: getattr(existing, field) for field in aliases} if existing else {
        "name": " ".join(filter(None, [user.first_name, user.last_name])),
        "email": user.email, "phone": user.phone or "",
        "phone_country_code": "44", "country": profile.country if profile else "GB",
        "address_line1": profile.address_line1 if profile else "",
        "address_line2": profile.address_line2 if profile else "",
        "city": profile.city if profile else "", "state": profile.county if profile else "",
        "postcode": profile.postcode if profile else "",
        "company": "", "company_number": "", "registrant_type": "",
    }
    values.update({field: value for field, value in supplied.items() if value})
    values["label"] = "Staff registration"
    form = DomainContactForm(values)
    if not form.is_valid():
        raise ValidationError([
            f"{form.fields[field].label or field.replace('_', ' ').title()}: {message}"
            for field, errors in form.errors.items() for message in errors
        ])
    contact = form.save(commit=False)
    contact.user = user
    contact.is_default = not DomainContact.objects.filter(user=user).exists()
    contact.as_resellerclub_payload("1", tld)
    contact.save()
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
    if len(name) > 100 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("Use a WHM package name of at most 100 characters without control characters.")

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

    from apps.domains.contact_codes import COUNTRY_CHOICES, COUNTRY_CODES
    from apps.domains.us_registry import NEXUS_CHOICES, PURPOSE_CHOICES
    context = {
        "us_nexus_choices": NEXUS_CHOICES, "us_purpose_choices": PURPOSE_CHOICES,
        "country_choices": COUNTRY_CHOICES,
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
    for field in ("registrant_type", "company", "company_number", "phone_country_code",
                  "address_line2", "state", "country"):
        form[f"contact_{field}"] = str(request.POST.get(f"contact_{field}") or "").strip()
    context["form"] = form
    form["contact_country"] = form["contact_country"].upper()
    context["invalid_contact_country"] = bool(form["contact_country"] and form["contact_country"] not in COUNTRY_CODES)

    try:
        years = max(1, min(10, int(form["years"])))
    except ValueError:
        years = 1

    selected_user = form["user_id"]
    valid_user_id = (
        bool(selected_user) and len(selected_user) <= 19
        and selected_user.isascii() and selected_user.isdigit()
        and int(selected_user) <= 2**63 - 1
    )
    user = User.objects.filter(pk=selected_user, is_active=True).first() if valid_user_id else None
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
        .order_by("-id")
        .first()
    )
    if blocking:
        messages.error(
            request,
            f"A platform order already exists for {domain_name} "
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

    from django.core.exceptions import ValidationError
    try:
        contact = _ensure_contact(user, request.POST, tld)
        from apps.domains.registry_requirements import validate_registration
        validate_registration(domain_name, contact, years)
    except ValidationError as exc:
        context["contact_errors"] = exc.messages
        return render(request, "admin_tools/manual_domain_register.html", context)
    price = _price_for_tld(tld, years)

    from apps.domains.registrar_policy import policy_for
    from .manual_followups import capture_manual_followup
    try:
        with transaction.atomic():
            order = DomainOrder.objects.create(
                user=user,
                invoice=None,
                domain_name=domain_name,
                tld=tld,
                registration_years=years,
                quoted_price=price,
                total_price=price,
                status=DomainOrder.STATUS_PAID,
                privacy_enabled=form["privacy_enabled"] and policy_for(tld).privacy_supported,
                auto_renew=form["auto_renew"],
                dns_provider=Domain.DNS_PROVIDER_CPANEL if form["create_hosting"] else Domain.DNS_PROVIDER_REGISTRAR,
                registration_contact=contact,
                admin_contact=contact,
                tech_contact=contact,
                billing_contact=contact,
                last_error="",
            )
            capture_manual_followup(order=order, actor=request.user, hosting=form["create_hosting"], package=package,
                invoice=form["create_invoice"], invoice_amount=form["invoice_amount"], notes=form["notes"])
    except ValidationError as exc:
        context["contact_errors"] = exc.messages
        return render(request, "admin_tools/manual_domain_register.html", context)
    messages.info(request,
        "The manual request and follow-up choices are saved. The customer must sign in and open "
        "My domains > Request agreements to accept the applicable terms. Staff can then process this same order; "
        "saved hosting and invoice choices will resume after registration.")
    return redirect("admin_tools:domain_order_detail", pk=order.pk)


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
@transaction.atomic
def manual_domain_generate_invoice(request, order_id):
    """Create an unpaid invoice after the fact for a manual domain order."""
    order = get_object_or_404(DomainOrder.objects.select_for_update(of=("self",)).select_related("user", "domain", "invoice"), pk=order_id)
    if order.invoice_id:
        messages.info(request, f"Order already has invoice {order.invoice.number}.")
        return redirect("admin_tools:invoice_edit", pk=order.invoice_id)
    if order.domain_id and (order.domain.user_id != order.user_id or order.domain.name != order.domain_name):
        messages.error(request, "The domain association needs support review before invoicing.")
        return redirect("admin_tools:domain_order_detail", pk=order.pk)

    if order.status != DomainOrder.STATUS_COMPLETED or not order.domain_id:
        attempt = getattr(order, "registration_attempt", None)
        if attempt and (attempt.status != "aborted" or attempt.submitted_at or attempt.legacy_review_required):
            messages.error(request, "Review the registrar submission before changing this order's invoice association.")
            return redirect("admin_tools:domain_order_detail", pk=order.pk)
        from apps.domains.agreements import assert_acceptance_current
        try:
            from apps.domains.request_agreements import request_agreement_target
            from apps.domains.registration_checkout import validate_registration_invoice_source
            order, contact, _, _ = request_agreement_target(order.user, order.pk, kind="registration", lock=True)
            User.objects.select_for_update().get(pk=order.user_id)
            validate_registration_invoice_source(order)
            assert_acceptance_current(receipt_id=order.agreement_acceptance_id, customer_id=order.user_id,
                contact_id=order.registration_contact_id, domain_name=order.domain_name, operation="registration")
        except ValidationError as exc:
            messages.error(request, "Customer agreement review is required before creating this invoice. " + " ".join(exc.messages))
            return redirect("admin_tools:domain_order_detail", pk=order.pk)

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
