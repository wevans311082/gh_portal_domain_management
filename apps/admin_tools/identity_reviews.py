"""Owner-only, recent-MFA recording of independent local policy outcomes."""

import uuid

from django import forms
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.domains.identity_review import record_identity_review
from apps.domains.models import ContactVerification, DomainContact
from .customer_workspace import customer_record
from .decorators import staff_member_required


class IdentityReviewForm(forms.Form):
    key = forms.UUIDField(widget=forms.HiddenInput)
    subject_digest = forms.RegexField(regex=r"^[0-9a-f]{64}$", widget=forms.HiddenInput)
    status = forms.ChoiceField(label="Local policy outcome", choices=[("pending", "Pending review"),
        ("verified", "Local policy review passed"), ("rejected", "Local policy review rejected"), ("unknown", "Outcome not established")])
    policy_reference = forms.RegexField(regex=r"^[A-Za-z0-9._:/-]{1,100}$", max_length=100, label="Local policy reference",
        help_text="Use the identifier and version of your approved local review policy. This form does not define a document requirement.")
    evidence_reference = forms.RegexField(regex=r"^[A-Za-z0-9._:/-]{1,100}$", max_length=100, label="Review case reference",
        help_text="Enter a case reference only. Keep identity documents, personal identifiers and credentials out of this form.")
    valid_days = forms.IntegerField(min_value=1, max_value=365, initial=90, label="Review validity in days",
        help_text="Use the referenced policy's validity period, up to 365 days. Expiry invalidates the outcome; it does not delete the audit history.")
    policy_reviewed = forms.BooleanField(label="I completed the referenced local policy review for the contact version shown.")

    def clean(self):
        cleaned = super().clean()
        if hasattr(self.data, "getlist") and any(len(self.data.getlist(key)) != 1 for key in self.data):
            raise forms.ValidationError("Submit each field once.")
        if self.errors.get("key") or self.errors.get("subject_digest"):
            self._errors.pop("key", None)
            self._errors.pop("subject_digest", None)
            raise forms.ValidationError("Reload this page to obtain a current review form before submitting.")
        return cleaned


@staff_member_required
@require_http_methods(["GET", "POST"])
def identity_review(request, pk, contact_pk):
    customer = customer_record(pk)
    contact = get_object_or_404(DomainContact, pk=contact_pk, user=customer)
    form = IdentityReviewForm(request.POST if request.method == "POST" else None,
        initial={"key": uuid.uuid4(), "subject_digest": ContactVerification.digest_for(contact)})
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                record_identity_review(actor=request.user, contact_id=contact.pk, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, " ".join(exc.messages))
            else:
                return redirect("admin_tools:identity_review", pk=customer.pk, contact_pk=contact.pk)
        status = 400
    history = Paginator(ContactVerification.objects.filter(contact=contact, check_kind="identity").select_related("reviewer"), 20).get_page(request.GET.get("page"))
    return render(request, "admin_tools/identity_review.html", {"customer": customer, "contact": contact,
        "form": form, "page_obj": history}, status=status)
