from django import forms
from django.utils import timezone

from apps.domains.models import Domain, DomainContact
from apps.companies.services import CompaniesHouseService

_INPUT = "block w-full rounded-md border-gray-300 shadow-sm focus:border-sky-500 focus:ring-sky-500 sm:text-sm"
_SELECT = _INPUT


class DomainContactForm(forms.ModelForm):
    validate_company_with_companies_house = forms.BooleanField(required=False, initial=True)

    class Meta:
        model = DomainContact
        fields = [
            "label", "name", "company", "company_number",
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

        if company and should_validate and company_number:
            payload = CompaniesHouseService().get_company(company_number)
            if not payload:
                self.add_error(
                    "company_number",
                    "Could not verify this company number with Companies House."
                )
            else:
                self._companies_house_payload = payload
        elif company and should_validate and not company_number:
            self.add_error("company_number", "Enter a company number for company validation.")

        return cleaned

    def save(self, commit=True):
        contact = super().save(commit=False)
        payload = self._companies_house_payload

        if payload:
            contact.registrant_validation_status = DomainContact.VALIDATION_VALIDATED
            contact.registrant_validated_at = timezone.now()
            contact.registrant_validation_notes = "Validated via Companies House."
        elif contact.company and contact.company_number:
            contact.registrant_validation_status = DomainContact.VALIDATION_PENDING
            contact.registrant_validated_at = None
            contact.registrant_validation_notes = "Company details provided. Pending manual/registrar validation."
        elif contact.company and not contact.company_number:
            contact.registrant_validation_status = DomainContact.VALIDATION_REJECTED
            contact.registrant_validated_at = None
            contact.registrant_validation_notes = "Company name provided without company number."
        else:
            contact.registrant_validation_status = DomainContact.VALIDATION_VALIDATED
            contact.registrant_validated_at = timezone.now()
            contact.registrant_validation_notes = "Validated as individual registrant details."

        if commit:
            contact.save()
        return contact


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
            lambda obj: f"{obj.label} ({obj.get_registrant_validation_status_display()})"
        )

    def clean_contact(self):
        contact = self.cleaned_data["contact"]
        if contact.registrant_validation_status != DomainContact.VALIDATION_VALIDATED:
            raise forms.ValidationError(
                "Selected contact is not registrant-validated yet. Update the contact and complete validation first."
            )
        return contact

    def clean_domain_name(self):
        return self.cleaned_data["domain_name"].strip().lower()
