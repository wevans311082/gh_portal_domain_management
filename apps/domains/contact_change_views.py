"""Private customer admission and bounded progress for contact replacements."""

import uuid

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.domains.contact_changes import OPEN_STATUSES, locked_source, request_contact_change
from apps.domains.models import Domain, DomainContact, DomainContactChange
from apps.domains.publication_consent import CHOICES, DISCLOSURE
from apps.domains.registrar_policy import policy_for, tld_for_domain


class ContactChangeForm(forms.Form):
    key = forms.UUIDField(widget=forms.HiddenInput)
    publication_choice = forms.ChoiceField(label="Organization publication", choices=CHOICES, initial="redact",
        help_text=DISCLOSURE)
    confirm = forms.BooleanField(label="I am authorized to request these changes and have checked every proposed contact.")

    def __init__(self, *args, customer, domain, source=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.domain = domain
        self.roles = policy_for(tld_for_domain(domain.name)).roles
        for role in self.roles:
            self.fields[role] = forms.ModelChoiceField(label={"registration_contact": "Registrant contact",
                "admin_contact": "Administrative contact", "tech_contact": "Technical contact", "billing_contact": "Billing contact"}[role],
                queryset=DomainContact.objects.filter(user=customer).order_by("label", "pk"),
                initial=getattr(source, role + "_id", None), empty_label="Choose a contact")
        self.order_fields(["key", *self.roles, "publication_choice", "confirm"])
        for field in self.fields.values():
            if not isinstance(field.widget, (forms.HiddenInput, forms.CheckboxInput)):
                field.widget.attrs["class"] = "block w-full rounded-md border-slate-400 bg-white text-slate-900 shadow-sm focus:border-blue-700 focus:ring-blue-700"

    def clean(self):
        cleaned = super().clean()
        if hasattr(self.data, "getlist") and any(len(self.data.getlist(key)) != 1 for key in self.data):
            raise forms.ValidationError("Submit each field once.")
        if not self.errors:
            from apps.domains.publication_consent import validate_choice
            validate_choice(cleaned["publication_choice"], cleaned["registration_contact"], self.domain.name)
        return cleaned


@login_required
@require_http_methods(["GET", "POST"])
def contacts(request, pk):
    if not request.user.is_active or request.user.is_staff or request.user.is_superuser:
        raise PermissionDenied
    domain = get_object_or_404(Domain, pk=pk, user=request.user)
    source, unavailable = None, ""
    try:
        with transaction.atomic():
            locked = Domain.objects.select_for_update().get(pk=domain.pk, user=request.user)
            source, attempt, kind = locked_source(locked)
            if locked.status != Domain.STATUS_ACTIVE:
                raise ValidationError("Contact changes require an active domain.")
    except ValidationError as exc:
        unavailable = " ".join(exc.messages)
    form = None
    status = 200
    if not unavailable:
        form = ContactChangeForm(request.POST if request.method == "POST" else None,
            customer=request.user, domain=domain, source=source, initial={"key": uuid.uuid4()})
        if request.method == "POST":
            if form.is_valid():
                try:
                    request_contact_change(customer=request.user, domain_id=domain.pk, key=form.cleaned_data["key"],
                        contacts={role: form.cleaned_data[role].pk for role in form.roles},
                        publication_choice=form.cleaned_data["publication_choice"])
                except ValidationError as exc:
                    form.add_error(None, " ".join(exc.messages))
                else:
                    response = redirect("domains:contact_change", pk=domain.pk)
                    response["Cache-Control"] = "no-store"
                    return response
            status = 400
    elif request.method == "POST":
        status = 409
    changes = DomainContactChange.objects.filter(domain=domain, user=request.user).order_by("-created_at", "-pk")
    open_change = changes.filter(status__in=OPEN_STATUSES).first()
    page = Paginator(changes, 20).get_page(request.GET.get("page"))
    response = render(request, "domains/contact_change.html", {"domain": domain, "form": form,
        "unavailable": unavailable, "open_change": open_change, "page_obj": page,
        "show_form": form is not None and (open_change is None or request.method == "POST")}, status=status)
    response["Cache-Control"] = "no-store"
    return response
