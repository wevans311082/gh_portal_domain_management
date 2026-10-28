"""Permission-filtered customer workspace with bounded source queries."""

from datetime import datetime
from decimal import Decimal
import uuid

from django import forms
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Greatest
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from allauth.account.models import EmailAddress
from apps.accounts.models import User, ClientProfile
from apps.audit.models import AuditLog
from apps.billing.models import Invoice, Quote
from apps.companies.models import BusinessProfile
from apps.domains.models import Domain, DomainContact, ContactVerification
from apps.domains.verification import registrar_email_summary
from apps.services.models import Service
from apps.support.models import SupportTicket
from .access import is_owner, has_route_access, require_recent_mfa
from .decorators import staff_member_required
from .models import CustomerNote

PAGE_SIZE = 20
SALT = "admin.customer.timeline.v1"


def permits(actor, capability):
    return is_owner(actor) or actor.has_perm("admin_tools." + capability)


def customer_record(pk, *, lock=False):
    queryset = User.objects.filter(is_staff=False, is_superuser=False).only(
        "pk", "email", "first_name", "last_name", "is_active", "created_at", "mfa_enabled",
    )
    if lock:
        queryset = queryset.select_for_update()
    return get_object_or_404(queryset, pk=pk)


def visible_notes(actor, customer):
    audiences = ["general"]
    if permits(actor, "view_billing"):
        audiences.append("billing")
    if permits(actor, "view_audit"):
        audiences.append("security")
    return CustomerNote.objects.filter(customer=customer, audience__in=audiences)


class NoteForm(forms.Form):
    submission_key = forms.UUIDField(widget=forms.HiddenInput)
    body = forms.CharField(label="Internal note", max_length=4000, strip=True,
                           widget=forms.Textarea(attrs={"rows": 5, "maxlength": 4000}),
                           help_text="Staff only. Record service context, not passwords, payment details or identity documents. Maximum 4,000 characters.")

    audience = forms.ChoiceField(label="Note visibility", required=False,
        help_text="Customer staff notes must not include restricted financial or security information. Choose a restricted audience when needed.")

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("general", "Customer staff")]
        if permits(actor, "manage_billing") and permits(actor, "view_billing"):
            choices.append(("billing", "Billing staff"))
        if is_owner(actor):
            choices.append(("security", "Security auditors and owners"))
        self.fields["audience"].choices = choices

    def clean_audience(self):
        return self.cleaned_data["audience"] or "general"

    def clean_body(self):
        value = self.cleaned_data["body"]
        if any((ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in value):
            raise forms.ValidationError("Remove control characters from the note.")
        return value


def _page(queryset, number):
    text = str(number or "1")
    if not text.isascii() or not text.isdigit() or len(text) > 9:
        raise Http404
    return Paginator(queryset, PAGE_SIZE).get_page(text)


def sources(actor, customer):
    result = [("account", User.objects.filter(pk=customer.pk), "Account", "customers", ("email",))]
    definitions = (
        ("domains", "view_domains", Domain, "Domain", ("name", "status")),
        ("services", "view_services", Service, "Hosting service", ("domain_name", "status")),
        ("invoices", "view_billing", Invoice, "Invoice", ("number", "status")),
        ("quotes", "view_billing", Quote, "Quote", ("number", "status")),
        ("tickets", "view_support", SupportTicket, "Support ticket", ("subject", "status")),
    )
    for key, capability, model, label, fields in definitions:
        if permits(actor, capability):
            result.append((key, model.objects.filter(user_id=customer.pk), label, key, fields))
    return result


def timeline(actor, customer, token):
    boundary = None
    if token:
        try:
            if len(token) > 2048:
                raise ValueError
            boundary = signing.loads(token, salt=SALT, max_age=3600)
            at = datetime.fromisoformat(boundary["at"])
            if boundary["customer"] != customer.pk or not timezone.is_aware(at) or type(boundary["pk"]) is not int or boundary["pk"] < 1 or not isinstance(boundary["source"], str):
                raise ValueError
            boundary["at"] = at
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            raise forms.ValidationError("This activity page has expired or is invalid. Return to the latest activity.") from None
    events = []
    definitions = sources(actor, customer)
    definitions.append(("notes", visible_notes(actor, customer), "Internal note", "notes", ()))
    for key, queryset, label, kind, fields in definitions:
        # A source's creation and latest update are separate events. Intermediate
        # changes are not invented from current status or copied from raw logs.
        for field in ("created_at", "updated_at"):
            if key in {"notes", "account"} and field == "updated_at":
                continue
            source = key + ":" + field
            rows = queryset
            if field == "updated_at":
                rows = rows.exclude(updated_at=F("created_at"))
            if boundary:
                condition = Q(**{field + "__lt": boundary["at"]})
                if source < boundary["source"]:
                    condition |= Q(**{field: boundary["at"]})
                elif source == boundary["source"]:
                    condition |= Q(**{field: boundary["at"], "pk__lt": boundary["pk"]})
                rows = rows.filter(condition)
            for row in rows.order_by("-" + field, "-pk").values("pk", field, *fields)[:PAGE_SIZE + 1]:
                identifier = row.get("name") or row.get("domain_name") or row.get("number") or row.get("email") or (f"#{row['pk']}" if key != "notes" else "")
                url = reverse("admin_tools:customer_note", args=[customer.pk, row["pk"]]) if key == "notes" else reverse("admin_tools:search_record", args=[kind, row["pk"]])
                events.append({"at": row[field], "source": source, "pk": row["pk"], "url": url,
                               "title": f"{label} {identifier}".strip(),
                               "action": "Created" if field == "created_at" else "Last updated"})
    events.sort(key=lambda item: (item["at"], item["source"], item["pk"]), reverse=True)
    next_token = ""
    if len(events) > PAGE_SIZE:
        last = events[PAGE_SIZE - 1]
        next_token = signing.dumps({"at": last["at"].isoformat(), "source": last["source"], "pk": last["pk"], "customer": customer.pk}, salt=SALT, compress=True)
    return events[:PAGE_SIZE], next_token


def workspace_context(request, customer, form=None):
    actor = request.user
    section = request.GET.get("section", "")
    available = sources(actor, customer)[1:]
    if section and section not in {item[0] for item in available}:
        raise Http404
    panels = []
    for key, queryset, label, kind, fields in available:
        display_fields = ("pk", "user_id", "created_at", *fields, *(("tld",) if key == "domains" else ()))
        page = _page(queryset.order_by("-created_at", "-pk").only(*display_fields), request.GET.get("page", "1") if section == key else "1")
        items = []
        for record in page if section == key else list(page)[:5]:
            title = getattr(record, "name", "") or getattr(record, "domain_name", "") or getattr(record, "number", "") or f"#{record.pk}"
            item = {"title": title, "status": record.get_status_display(), "url": reverse("admin_tools:search_record", args=[kind, record.pk])}
            if key == "tickets":
                item["description"] = record.subject
            if key == "domains":
                item["verification"] = registrar_email_summary(record)
            items.append(item)
        panels.append({"key": key, "label": label, "count": page.paginator.count, "items": items,
                       "page": page if section == key else None})
    profile = ClientProfile.objects.filter(user=customer).first()
    context = {
        "customer": customer, "customer_kind": ("Business" if profile.is_business else "Individual") if profile else "Not confirmed",
        "business_profile": BusinessProfile.objects.filter(user=customer).only("company_name", "company_number").first(),
        "portal_email_verified": EmailAddress.objects.filter(user=customer, email__iexact=customer.email, verified=True).exists(),
        "panels": panels, "section": section, "show_security": permits(actor, "view_audit"),
        "can_add_note": permits(actor, "manage_customer_notes"),
        "note_form": form or NoteForm(actor=actor, initial={"submission_key": uuid.uuid4()}),
        "notes": _page(visible_notes(actor, customer).select_related("author"), request.GET.get("notes_page", "1")),
        "can_redact": is_owner(actor),
        "can_manage_customer": has_route_access(actor, "user_edit"),
    }
    if permits(actor, "view_billing"):
        money = DecimalField(max_digits=20, decimal_places=2)
        context["balances"] = list(Invoice.objects.filter(user=customer, status__in=["unpaid", "overdue", "partially_paid"]).values("currency").annotate(
            outstanding=Sum(Greatest(F("total") - F("amount_paid"), Value(Decimal("0.00")), output_field=money)),
        ).order_by("currency")[:21])
        context["balances_truncated"] = len(context["balances"]) > 20
        context["balances"] = context["balances"][:20]
    if permits(actor, "view_support"):
        context["open_tickets"] = SupportTicket.objects.filter(user=customer).exclude(status__in=["resolved", "closed"]).count()
    if permits(actor, "view_domains"):
        contacts = []
        for contact in DomainContact.objects.filter(user=customer).order_by("-updated_at", "-pk")[:6]:
            try:
                # Local completeness is not proof of identity or TLD eligibility.
                contact.as_resellerclub_payload("1", "com")
                data_label = "Complete for general contact fields"
            except forms.ValidationError:
                data_label = "Needs contact-data review"
            evidence = ContactVerification.objects.filter(contact=contact, check_kind="business").first()
            business = "Not applicable" if contact.registrant_type == "individual" else "Not confirmed"
            if contact.registrant_type == "organisation" and evidence and evidence.is_current:
                business = evidence.get_status_display()
            contacts.append({"label": contact.label, "kind": contact.get_registrant_type_display(), "data": data_label, "business": business})
        context["contacts_truncated"] = len(contacts) > 5
        context["contact_checks"] = contacts[:5]
    try:
        context["activity"], context["next_cursor"] = timeline(actor, customer, request.GET.get("cursor", ""))
    except forms.ValidationError as exc:
        context["activity_error"] = exc.messages[0]
        context["activity"] = []
    return context


@staff_member_required
@require_GET
def customer_workspace(request, pk):
    return render(request, "admin_tools/customer_workspace.html", workspace_context(request, customer_record(pk)))


@staff_member_required
@require_POST
def customer_note_add(request, pk):
    if not permits(request.user, "view_customers"):
        raise PermissionDenied
    customer = customer_record(pk)
    form = NoteForm(request.POST, actor=request.user)
    if form.is_valid():
        audience = form.cleaned_data["audience"]
        if audience == "security":
            challenge = require_recent_mfa(request)
            if challenge is not None:
                return challenge
        with transaction.atomic():
            customer = customer_record(pk, lock=True)
            actor = User.objects.select_for_update().filter(pk=request.user.pk).first()
            if actor is None or not actor.is_active or not actor.is_staff or not permits(actor, "manage_customer_notes") or not permits(actor, "view_customers"):
                raise PermissionDenied
            if (
                audience == "billing" and not (permits(actor, "manage_billing") and permits(actor, "view_billing"))
                or audience == "security" and not is_owner(actor)
            ):
                raise PermissionDenied
            if audience == "security":
                request.user = actor
                challenge = require_recent_mfa(request)
                if challenge is not None:
                    return challenge
            note, created = CustomerNote.objects.get_or_create(submission_key=form.cleaned_data["submission_key"], defaults={
                "customer": customer, "author": actor, "body": form.cleaned_data["body"], "audience": audience,
            })
            if note.customer_id != customer.pk or note.author_id != actor.pk or note.body != form.cleaned_data["body"] or note.redacted_at or note.audience != audience:
                return HttpResponseBadRequest("This submission key belongs to another note. Reload the workspace and try again.")
            if created:
                AuditLog.objects.create(user=actor, action="customer.note.added", model_name="CustomerNote", object_id=str(note.pk), data={"customer_id": customer.pk, "audience": audience})
        return redirect(reverse("admin_tools:customer_workspace", args=[pk]) + "#internal-notes")
    return render(request, "admin_tools/customer_workspace.html", workspace_context(request, customer, form), status=400)


@staff_member_required
@require_GET
def customer_note(request, pk, note_pk):
    customer = customer_record(pk)
    note = get_object_or_404(visible_notes(request.user, customer).select_related("author"), pk=note_pk)
    return render(request, "admin_tools/customer_note.html", {"customer": customer, "note": note, "can_redact": is_owner(request.user)})


@staff_member_required
@require_POST
def customer_note_redact(request, pk, note_pk):
    with transaction.atomic():
        customer = customer_record(pk, lock=True)
        actor = User.objects.select_for_update().filter(pk=request.user.pk).first()
        if actor is None or not is_owner(actor):
            raise PermissionDenied
        request.user = actor
        challenge = require_recent_mfa(request)
        if challenge is not None:
            return challenge
        note = get_object_or_404(CustomerNote.objects.select_for_update(), customer=customer, pk=note_pk)
        if note.redacted_at is None:
            note.body = ""
            note.redacted_at = timezone.now()
            note.redacted_by = request.user
            note.save(update_fields=["body", "redacted_at", "redacted_by", "updated_at"])
            AuditLog.objects.create(user=request.user, action="customer.note.redacted", model_name="CustomerNote", object_id=str(note.pk), data={"customer_id": customer.pk})
    return redirect("admin_tools:customer_note", pk=pk, note_pk=note_pk)
