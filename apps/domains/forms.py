from django import forms

from apps.domains.models import Domain, DomainContact

_INPUT = "block w-full rounded-md border-gray-300 shadow-sm focus:border-sky-500 focus:ring-sky-500 sm:text-sm"
_SELECT = _INPUT


class DomainContactForm(forms.ModelForm):
    class Meta:
        model = DomainContact
        fields = [
            "label", "name", "company",
            "email", "phone_country_code", "phone",
            "address_line1", "address_line2", "city", "state", "postcode", "country",
            "is_default",
        ]
        widgets = {
            "label":             forms.TextInput(attrs={"class": _INPUT, "placeholder": "e.g. Primary Contact"}),
            "name":              forms.TextInput(attrs={"class": _INPUT, "placeholder": "Full name"}),
            "company":           forms.TextInput(attrs={"class": _INPUT, "placeholder": "Optional"}),
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

    def clean_country(self):
        val = self.cleaned_data.get("country", "").strip().upper()
        if len(val) != 2:
            raise forms.ValidationError("Enter a 2-letter ISO country code, e.g. GB or US.")
        return val


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

    def clean_domain_name(self):
        return self.cleaned_data["domain_name"].strip().lower()
