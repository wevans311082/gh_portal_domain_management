from django import forms

from apps.domains.models import Domain, DomainContact


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
