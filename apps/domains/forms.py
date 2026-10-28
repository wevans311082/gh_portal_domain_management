from django import forms
from django.db import transaction
from django.utils import timezone

from apps.domains.models import ContactVerification, Domain, DomainContact
from apps.companies.services import CompaniesHouseService
from apps.domains.contact_codes import CountryChoiceField

_INPUT = "block w-full rounded-md border-gray-300 shadow-sm focus:border-sky-500 focus:ring-sky-500 sm:text-sm"
_SELECT = _INPUT


class VerificationActionForm(forms.Form):
    key = forms.UUIDField()
    kind = forms.ChoiceField(choices=[("refresh", "Refresh"), ("resend", "Resend")])

    def clean(self):
        cleaned = super().clean()
        if hasattr(self.data, "getlist"):
            if any(len(self.data.getlist(key)) != 1 for key in self.data):
                raise forms.ValidationError("Submit each field once.")
        return cleaned


class DomainContactForm(forms.ModelForm):
    country = CountryChoiceField(
        label="Country or territory", widget=forms.Select(attrs={"class": _SELECT}),
        help_text="Choose the contact address country. GB is the United Kingdom. This does not establish citizenship or TLD eligibility.",
    )
    registrant_type = forms.ChoiceField(
        choices=[("", "Choose a registrant type"), ("individual", "Individual"),
                 ("organisation", "Organisation or sole trader")],
        widget=forms.Select(attrs={"class": _SELECT}),
    )
    validate_company_with_companies_house = forms.BooleanField(required=False, initial=False)

    class Meta:
        model = DomainContact
        fields = [
            "label", "registrant_type", "name", "company", "company_number",
            "email", "phone_country_code", "phone",
            "address_line1", "address_line2", "city", "state", "postcode", "country",
            "is_default",
        ]
        widgets = {
            "label":             forms.TextInput(attrs={"class": _INPUT, "placeholder": "e.g. Primary Contact"}),
            "name":              forms.TextInput(attrs={"class": _INPUT, "placeholder": "Full name"}),
            "company":           forms.TextInput(attrs={"class": _INPUT, "placeholder": "Optional"}),
            "company_number":    forms.TextInput(attrs={"class": _INPUT, "placeholder": "UK company number (optional)"}),
            "email":             forms.EmailInput(attrs={"class": _INPUT}),
            "phone_country_code": forms.TextInput(attrs={"class": _INPUT, "placeholder": "44"}),
            "phone":             forms.TextInput(attrs={"class": _INPUT, "placeholder": "07700900000"}),
            "address_line1":     forms.TextInput(attrs={"class": _INPUT}),
            "address_line2":     forms.TextInput(attrs={"class": _INPUT, "placeholder": "Optional"}),
            "city":              forms.TextInput(attrs={"class": _INPUT}),
            "state":             forms.TextInput(attrs={"class": _INPUT, "placeholder": "County / State"}),
            "postcode":          forms.TextInput(attrs={"class": _INPUT}),
            "country":           forms.TextInput(attrs={"class": _INPUT, "placeholder": "GB"}),
            "is_default":        forms.CheckboxInput(attrs={"class": "rounded border-gray-300 text-sky-600 focus:ring-sky-500"}),
        }
        labels = {
            "label":             "Contact label",
            "name":              "Full name",
            "company":           "Company",
            "company_number":    "Company number",
            "email":             "Email address",
            "phone_country_code": "Phone country code",
            "phone":             "Phone number",
            "address_line1":     "Address line 1",
            "address_line2":     "Address line 2",
            "city":              "City",
            "state":             "County / State",
            "postcode":          "Postcode / ZIP",
            "country":           "Country (ISO 2-letter)",
            "is_default":        "Set as my default contact",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._companies_house_payload = None
        self.fields["phone_country_code"].help_text = "Use an assigned international calling code without +, for example 44 or 1. It may differ from the address country."
        if self.initial.get("registrant_type") == "unspecified":
            self.initial["registrant_type"] = ""

    def clean_company_number(self):
        raw = (self.cleaned_data.get("company_number") or "").strip()
        return raw.replace(" ", "").upper()

    def clean_country(self):
        val = self.cleaned_data.get("country", "").strip().upper()
        if len(val) != 2:
            raise forms.ValidationError("Enter a 2-letter ISO country code, e.g. GB or US.")
        return val

    def clean(self):
        cleaned = super().clean()
        company = (cleaned.get("company") or "").strip()
        company_number = cleaned.get("company_number") or ""
        should_validate = cleaned.get("validate_company_with_companies_house")
        self._companies_house_payload = None

        kind = cleaned.get("registrant_type")
        if kind == "individual" and (company or company_number):
            self.add_error("registrant_type", "Choose an organisation for business details, or clear the company fields for an individual.")
        if kind == "organisation" and not company:
            self.add_error("company", "Enter the organisation or trading name.")
        if should_validate and kind != "organisation":
            self.add_error("validate_company_with_companies_house", "Companies House checks apply only to UK incorporated organisations.")

        # Apply the same general API bounds at data entry, before a paid order.
        from django.core.exceptions import ValidationError
        candidate = DomainContact(**{
            key: value for key, value in cleaned.items() if key in self.Meta.fields
        })
        try:
            candidate.as_resellerclub_payload("1")
        except ValidationError as exc:
            for key, messages in exc.message_dict.items():
                if key in self.fields and key in cleaned:
                    for message in messages:
                        self.add_error(key, message)

        if not self.errors and kind == "organisation" and company and should_validate and company_number:
            payload = CompaniesHouseService().get_company(company_number)
            if not isinstance(payload, dict) or not payload:
                self.add_error(
                    "company_number",
                    "Could not verify this company number with Companies House."
                )
            elif (
                str(payload.get("company_number", "")).upper() != company_number
                or " ".join(str(payload.get("company_name", "")).casefold().split())
                != " ".join(company.casefold().split())
            ):
                self.add_error("company", "The company name and number must match the Companies House record.")
            else:
                self._companies_house_payload = payload
        elif kind == "organisation" and company and should_validate and not company_number:
            self.add_error("company_number", "Enter a company number for company validation.")

        return cleaned

    def save(self, commit=True):
        contact = super().save(commit=False)
        # Saving supplied details establishes neither identity nor registrar approval.
        contact.registrant_validation_status = DomainContact.VALIDATION_UNVALIDATED
        contact.registrant_validated_at = None
        contact.registrant_validation_notes = (
            "Contact details saved. Registrar and applicable registry checks remain separate."
        )

        if commit:
            with transaction.atomic():
                contact.save()
                self.save_business_evidence(contact)
        return contact

    def save_business_evidence(self, contact):
        """Call after saving when using save(commit=False), in the same transaction."""
        if self._companies_house_payload:
            ContactVerification.objects.create(
                contact=contact, check_kind="business", status="verified",
                subject_digest=ContactVerification.digest_for(contact),
                source="companies_house", reference=contact.company_number,
                checked_at=timezone.now(),
            )


class DomainRegistrationForm(forms.Form):
    domain_name = forms.CharField(max_length=255, widget=forms.HiddenInput)
    registration_years = forms.TypedChoiceField(
        choices=[(1, "1 year"), (2, "2 years"), (3, "3 years")],
        coerce=int,
        initial=1,
    )
    contact = forms.ModelChoiceField(queryset=DomainContact.objects.none())
    dns_provider = forms.ChoiceField(
        choices=[
            (Domain.DNS_PROVIDER_CPANEL, "Use platform nameservers"),
            (Domain.DNS_PROVIDER_CLOUDFLARE, "Use Cloudflare with managed www"),
        ],
        initial=Domain.DNS_PROVIDER_CPANEL,
    )
    privacy_enabled = forms.BooleanField(required=False, initial=True)
    auto_renew = forms.BooleanField(required=False, initial=True)

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user")
        super().__init__(*args, **kwargs)
        self.fields["contact"].queryset = DomainContact.objects.filter(user=user).order_by("label")
        self.fields["contact"].label_from_instance = (
            lambda obj: f"{obj.label} ({obj.get_registrant_type_display()})"
        )
        from apps.domains.registrar_policy import policy_for, tld_for_domain
        domain_hint = self.data.get("domain_name") if self.is_bound else self.initial.get("domain_name")
        self.policy_review_message = ""
        if domain_hint:
            try:
                policy = policy_for(tld_for_domain(domain_hint))
            except forms.ValidationError as exc:
                self.policy_review_message = " ".join(exc.messages)
            else:
                if not policy.privacy_supported:
                    self.fields["privacy_enabled"].disabled = True
                    self.fields["privacy_enabled"].initial = False
                    self.initial["privacy_enabled"] = False
                    self.fields["privacy_enabled"].help_text = "Registrar privacy protection is not supported for this extension."

    def clean_contact(self):
        contact = self.cleaned_data["contact"]
        if contact.registrant_type not in {"individual", "organisation"}:
            raise forms.ValidationError(
                "Edit this contact and confirm whether the registrant is an individual or organisation."
            )
        # Registrar email verification may begin only after registration. Local
        # legacy approval cannot replace accurate data or prove provider approval.
        from django.core.exceptions import ValidationError
        try:
            contact.as_resellerclub_payload("1")
        except ValidationError:
            raise forms.ValidationError("Update this contact with complete, valid registrar contact details.")
        return contact

    def clean_domain_name(self):
        return self.cleaned_data["domain_name"].strip().lower()

    def clean(self):
        cleaned = super().clean()
        domain = cleaned.get("domain_name")
        if domain:
            from apps.domains.registrar_policy import policy_for, tld_for_domain
            try:
                policy = policy_for(tld_for_domain(domain))
            except forms.ValidationError as exc:
                self.add_error("domain_name", exc)
            else:
                if not policy.privacy_supported:
                    cleaned["privacy_enabled"] = False
        return cleaned
