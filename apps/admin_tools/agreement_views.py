"""Owner-only, recent-MFA review and publication of immutable agreement text."""

import uuid

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from apps.domains.agreements import DOCUMENT_FIELDS, OPERATIONS, agreement_document_is_intact, publication_preview_digest, publish_agreement
from apps.domains.models import RegistrarAgreementCatalogue, RegistrarAgreementVersion
from apps.domains.registrar_policy import PROFILE_TLDS
from .decorators import staff_member_required

PREVIEW_SALT = "registrar-agreement-owner-preview-v1"


def profile_labels():
    labels = {"general_raa": "General contacts / ICANN RAA", "general_registry": "General contacts / registry-specific verification",
              "uk_accuracy": "UK registrant requirements", "us_nexus": "US nexus requirements"}
    return {key: labels.get(key, key) + " (" + ", ".join("." + tld for tld in members) + ")"
            for key, (policy, members) in PROFILE_TLDS.items()}


class AgreementPublicationForm(forms.Form):
    submission_key = forms.UUIDField(widget=forms.HiddenInput)
    preview_token = forms.CharField(required=False, max_length=2048, widget=forms.HiddenInput)
    agreement_key = forms.RegexField(regex=r"^[a-z][a-z0-9-]{0,79}$", max_length=80, initial="domain-service",
        help_text="Use the configured requirement key. Publishing another key does not add a new requirement by itself.")
    version = forms.RegexField(regex=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$", max_length=80,
        help_text="Use a new version reference. Existing published versions cannot be overwritten.")
    title = forms.CharField(max_length=200)
    body = forms.CharField(label="Approved agreement text", max_length=20000, strip=False,
        widget=forms.Textarea(attrs={"rows": 14}), help_text="Paste the exact approved text. It is displayed as plain text; HTML is not executed.")
    source_url = forms.URLField(label="Source reference", required=False, max_length=500, assume_scheme="https",
        help_text="Optional HTTPS reference, without credentials. The application does not fetch this address.")
    profile_keys = forms.MultipleChoiceField(label="Shared extension groups", widget=forms.CheckboxSelectMultiple)
    operations = forms.MultipleChoiceField(label="Operations", choices=[(key, key.title()) for key in OPERATIONS], widget=forms.CheckboxSelectMultiple)
    reviewed = forms.BooleanField(required=False,
        label="I have reviewed and approved the exact previewed text, extension groups and operations for publication.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["profile_keys"].choices = list(profile_labels().items())

    def clean(self):
        cleaned = super().clean()
        if set(self.data) - set(self.fields) - {"action", "csrfmiddlewaretoken"}:
            raise forms.ValidationError("Submit only the displayed agreement fields.")
        if hasattr(self.data, "getlist"):
            for key in self.data:
                values = self.data.getlist(key)
                if key in {"profile_keys", "operations"}:
                    if len(set(values)) != len(values):
                        raise forms.ValidationError("Select each group and operation once.")
                elif len(values) != 1:
                    raise forms.ValidationError("Submit each field once.")
        if self.errors.get("submission_key") or self.errors.get("preview_token"):
            self._errors.pop("submission_key", None)
            self._errors.pop("preview_token", None)
            raise forms.ValidationError("Reload this page to obtain a fresh publication form.")
        return cleaned


def _history():
    return RegistrarAgreementVersion.objects.select_related("publisher").defer("body").prefetch_related("current_bindings")


@staff_member_required
@require_http_methods(["GET", "POST"])
def registrar_agreements(request):
    form = AgreementPublicationForm(request.POST if request.method == "POST" else None,
        initial={"submission_key": uuid.uuid4()})
    preview = None
    status = 200
    if request.method == "POST":
        action = request.POST.get("action")
        if form.is_valid():
            if action not in {"preview", "publish"}:
                form.add_error(None, "Choose Preview agreement or Publish reviewed agreement.")
            else:
                data = {field: form.cleaned_data[field] for field in DOCUMENT_FIELDS}
                try:
                    digest = publication_preview_digest(**data)
                    expected = {"owner": request.user.pk, "key": str(form.cleaned_data["submission_key"]), "digest": digest,
                        "members": {key: sorted(PROFILE_TLDS[key][1]) for key in data["profile_keys"]}}
                    if action == "preview":
                        preview = data
                        shown = request.POST.copy()
                        revision = RegistrarAgreementCatalogue.objects.filter(pk=1).values_list("revision", flat=True).first() or 0
                        shown["preview_token"] = signing.dumps(dict(expected, revision=revision), salt=PREVIEW_SALT, compress=True)
                        shown["reviewed"] = ""
                        form = AgreementPublicationForm(shown)
                        form.is_valid()
                    else:
                        try:
                            token = signing.loads(form.cleaned_data["preview_token"], salt=PREVIEW_SALT, max_age=1800)
                        except (signing.BadSignature, ValueError, TypeError):
                            raise ValidationError("Preview the current text and scope again before publishing.") from None
                        if (not isinstance(token, dict) or set(token) != set(expected) | {"revision"}
                                or type(token.get("revision")) is not int or token["revision"] < 0
                                or {key: token[key] for key in expected} != expected):
                            raise ValidationError("The text or scope changed after preview. Preview it again before publishing.")
                        if not form.cleaned_data["reviewed"]:
                            form.add_error("reviewed", "Confirm approval of the exact preview before publishing.")
                            preview = data
                        else:
                            document = publish_agreement(actor=request.user, submission_key=form.cleaned_data["submission_key"],
                                reviewed=True, expected_revision=token["revision"], **data)
                            return redirect("admin_tools:registrar_agreement_detail", pk=document.pk)
                except ValidationError as exc:
                    form.add_error(None, " ".join(exc.messages))
        if form.errors:
            status = 400
    page = Paginator(_history(), 20).get_page(request.GET.get("page"))
    labels = profile_labels()
    rows = [{"document": document, "bindings": [
        {"profile": labels.get(binding.profile_key, binding.profile_key), "operation": binding.operation.title()}
        for binding in document.current_bindings.all()]} for document in page]
    return render(request, "admin_tools/agreements/index.html", {"form": form, "preview": preview, "page_obj": page,
        "history_rows": rows, "preview_groups": [labels[key] for key in preview["profile_keys"]] if preview else []}, status=status)


@staff_member_required
@require_GET
def registrar_agreement_detail(request, pk):
    document = get_object_or_404(RegistrarAgreementVersion.objects.select_related("publisher"), pk=pk)
    intact = agreement_document_is_intact(document)
    labels = profile_labels()
    return render(request, "admin_tools/agreements/detail.html", {"document": document, "intact": intact,
        "source_reference": document.source_url if intact else "", "bindings": [
            {"profile": labels.get(binding.profile_key, binding.profile_key), "operation": binding.operation.title()}
            for binding in document.current_bindings.order_by("profile_key", "operation")]})
