from django import forms
from django.db import transaction
from django.utils import timezone

from apps.domains.models import ContactVerification, Domain, DomainContact
from apps.companies.services import CompaniesHouseService
from apps.domains.contact_codes import CountryChoiceField
from apps.domains.publication_consent import CHOICES, DISCLOSURE
from apps.domains.eu_registry import BASIS_CHOICES, ELIGIBLE_COUNTRIES
from apps.domains.contact_codes import COUNTRY_CHOICES

_INPUT = "block w-full rounded-md border-gray-300 shadow-sm focus:border-sky-500 focus:ring-sky-500 sm:text-sm"
_SELECT = _INPUT


class DomainNameserverForm(forms.Form):
    nameservers = forms.CharField(max_length=3400, widget=forms.Textarea(attrs={
        "rows": 5, "class": "font-mono", "spellcheck": "false",
        "data-no-field-hints": "", "placeholder": "ns1.example.com\nns2.example.com",
    }), help_text="Enter one hostname per line. The complete list replaces the current delegation; the registry limit applies (maximum 13).")

    def __init__(self, *args, domain_name, **kwargs):
        self.domain_name = domain_name
        super().__init__(*args, **kwargs)

    def clean_nameservers(self):
        from apps.domains.registry_requirements import validate_nameservers
        hosts = [line.strip().lower().rstrip(".") for line in self.cleaned_data["nameservers"].splitlines() if line.strip()]
        validate_nameservers(self.domain_name, hosts)
        return hosts


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
    eu_eligibility_basis = forms.ChoiceField(choices=BASIS_CHOICES, required=False, label="EU eligibility basis", widget=forms.Select(attrs={"class": _SELECT}))
    eu_citizenship_country = forms.ChoiceField(choices=[("", "Not needed unless citizenship is the chosen basis"),
        *((code, label) for code, label in COUNTRY_CHOICES if code in ELIGIBLE_COUNTRIES)], required=False,
        label="Country of citizenship for EU eligibility", widget=forms.Select(attrs={"class": _SELECT}))
    eu_eligibility_certified = forms.BooleanField(required=False, label="I certify this registrant meets the declared EU eligibility basis.")
    from apps.domains.us_registry import NEXUS_CHOICES, PURPOSE_CHOICES
    us_nexus_category = forms.ChoiceField(choices=NEXUS_CHOICES, required=False, label="US nexus category", widget=forms.Select(attrs={"class": _SELECT}))
    us_application_purpose = forms.ChoiceField(choices=PURPOSE_CHOICES, required=False, label="US intended use", widget=forms.Select(attrs={"class": _SELECT}))
    us_nexus_certified = forms.BooleanField(required=False, label="I certify this registrant's declared US nexus and intended use are accurate.")
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
            "us_nexus_category", "us_application_purpose", "us_nexus_certified",
            "eu_eligibility_basis", "eu_citizenship_country", "eu_eligibility_certified",
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
        self.initial["us_nexus_certified"] = False
        self.initial["eu_eligibility_certified"] = False
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
        if candidate.us_nexus_category or candidate.us_application_purpose or candidate.us_nexus_certified:
            from apps.domains.us_registry import validate_us_declaration
            try:
                validate_us_declaration(candidate)
            except ValidationError as exc:
                for key, errors in exc.message_dict.items():
                    for error in errors:
                        self.add_error(key, error)
        if candidate.eu_eligibility_basis or candidate.eu_citizenship_country or candidate.eu_eligibility_certified:
            from apps.domains.eu_registry import validate_eu_declaration
            try:
                validate_eu_declaration(candidate)
            except ValidationError as exc:
                for key, errors in exc.message_dict.items():
                    for error in errors:
                        self.add_error(key, error)
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
    publication_choice = forms.ChoiceField(choices=CHOICES, required=False, initial="redact",
        label="Organization publication for this request", help_text=DISCLOSURE)
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
        self.registry_guidance = ""
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
                from apps.domains.registry_requirements import UK_EXTENSIONS
                if tld_for_domain(domain_hint) == "us":
                    self.registry_guidance = "US registration requires an explicit, accurate nexus category and intended use on the registrant contact. Individuals do not need a company number. Privacy/proxy registrations are prohibited; actual registrant details may be published. Foreign-entity categories require support review."
                if tld_for_domain(domain_hint) in UK_EXTENSIONS:
                    self.registry_guidance = "UK domains use only the registrant contact and do not use registrar locks or EPP codes. Supply the actual full registrant name; individuals do not need a company number."
                    self.registry_guidance += " A sole trader registering as a person should select Individual and use their actual full name, not a trading name."
                    if tld_for_domain(domain_hint) == "me.uk":
                        self.registry_guidance += " .me.uk requires an individual registrant for a personal namespace. Exceptional registry decisions require support review."
                    elif tld_for_domain(domain_hint) == "org.uk":
                        self.registry_guidance += " .org.uk is intended for not-for-profit or public-service activities; this is not a company-number requirement."

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

    def clean_publication_choice(self):
        from apps.domains.publication_consent import posted_choice
        return posted_choice(self.data)

    def clean(self):
        cleaned = super().clean()
        domain = cleaned.get("domain_name")
        choice = cleaned.get("publication_choice") or "redact"
        cleaned["publication_choice"] = choice
        if domain:
            from apps.domains.publication_consent import validate_choice
            try:
                validate_choice(choice, cleaned.get("contact"), domain)
            except forms.ValidationError as exc:
                self.add_error("publication_choice", exc)
        if domain:
            from apps.domains.registrar_policy import policy_for, tld_for_domain
            try:
                policy = policy_for(tld_for_domain(domain))
            except forms.ValidationError as exc:
                self.add_error("domain_name", exc)
            else:
                if not policy.privacy_supported:
                    cleaned["privacy_enabled"] = False
                if cleaned.get("contact") and cleaned.get("registration_years"):
                    from apps.domains.registry_requirements import validate_registration
                    try:
                        validate_registration(domain, cleaned["contact"], cleaned["registration_years"])
                    except forms.ValidationError as exc:
                        self.add_error("contact", " ".join(exc.messages))
        return cleaned
