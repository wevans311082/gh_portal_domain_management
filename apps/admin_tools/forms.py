from django import forms
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from apps.accounts.models import User
from apps.companies.models import BusinessProfile
from apps.companies.services import CompaniesHouseService
from apps.core.models import (
    ErrorPageContent,
    HomeFAQ,
    HomeServiceCard,
    LegalPage,
    SiteContentSettings,
)
from apps.products.models import Package
from apps.domains.models import Domain
from apps.domains.models import DomainContact
from apps.services.models import Service
from apps.support.models import SupportTicket
from apps.payments.models import Payment
from apps.website_templates.models import WebsiteTemplate


class AdminUserCreateForm(forms.ModelForm):
    password1 = forms.CharField(widget=forms.PasswordInput)
    password2 = forms.CharField(widget=forms.PasswordInput)
    company_name = forms.CharField(max_length=255, required=False)
    company_number = forms.CharField(max_length=20, required=False)
    validate_company_with_companies_house = forms.BooleanField(required=False, initial=True)

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "phone", "is_active", "is_staff", "is_superuser"]

    def clean_password1(self):
        pwd = self.cleaned_data.get("password1")
        validate_password(pwd)
        return pwd

    def clean_password2(self):
        p1 = self.cleaned_data.get("password1")
        p2 = self.cleaned_data.get("password2")
        if p1 != p2:
            raise forms.ValidationError("Passwords do not match.")
        return p2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._companies_house_payload = None
        self.fields["company_name"].help_text = "Associate this user account with a business profile."
        self.fields["company_number"].help_text = "UK Companies House number (for example 00445790)."
        self.fields["validate_company_with_companies_house"].help_text = "Validate this number against Companies House on save."

    def clean_company_number(self):
        raw = (self.cleaned_data.get("company_number") or "").strip()
        return raw.replace(" ", "").upper()

    def clean(self):
        cleaned = super().clean()
        company_number = cleaned.get("company_number")
        should_validate = cleaned.get("validate_company_with_companies_house")
        self._companies_house_payload = None

        if company_number and should_validate:
            payload = CompaniesHouseService().get_company(company_number)
            if not payload:
                self.add_error(
                    "company_number",
                    "Company number could not be verified with Companies House. Check the number or API key.",
                )
            else:
                self._companies_house_payload = payload
        return cleaned

    def _build_registered_address(self, payload):
        address = (payload or {}).get("registered_office_address") or {}
        parts = [
            address.get("address_line_1", ""),
            address.get("address_line_2", ""),
            address.get("locality", ""),
            address.get("region", ""),
            address.get("postal_code", ""),
            address.get("country", ""),
        ]
        return "\n".join([p for p in parts if p]).strip()

    def _save_business_profile(self, user):
        company_name = (self.cleaned_data.get("company_name") or "").strip()
        company_number = (self.cleaned_data.get("company_number") or "").strip()
        payload = self._companies_house_payload

        has_data = bool(company_name or company_number)
        existing = BusinessProfile.objects.filter(user=user).first()
        if not has_data and not existing:
            return

        profile, _ = BusinessProfile.objects.get_or_create(
            user=user,
            defaults={"company_name": company_name or user.email},
        )

        if payload and not company_name:
            company_name = (payload.get("company_name") or "").strip() or company_name

        profile.company_name = company_name or profile.company_name or user.email
        profile.company_number = company_number
        profile.company_type = (payload or {}).get("type", "")
        profile.status = (payload or {}).get("company_status", "")
        profile.registered_address = self._build_registered_address(payload)
        profile.companies_house_data = payload or {}
        profile.is_verified = bool(payload)
        profile.verified_at = timezone.now() if payload else None
        profile.save()

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
            self._save_business_profile(user)
        return user


class AdminUserUpdateForm(forms.ModelForm):
    company_name = forms.CharField(max_length=255, required=False)
    company_number = forms.CharField(max_length=20, required=False)
    validate_company_with_companies_house = forms.BooleanField(required=False, initial=True)

    class Meta:
        model = User
        fields = [
            "email",
            "first_name",
            "last_name",
            "phone",
            "is_active",
            "is_staff",
            "is_superuser",
            "mfa_enabled",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._companies_house_payload = None
        self.fields["company_name"].help_text = "Associate this user account with a business profile."
        self.fields["company_number"].help_text = "UK Companies House number (for example 00445790)."
        self.fields["validate_company_with_companies_house"].help_text = "Validate this number against Companies House on save."

        profile = getattr(self.instance, "business_profile", None)
        if profile:
            self.fields["company_name"].initial = profile.company_name
            self.fields["company_number"].initial = profile.company_number

    def clean_company_number(self):
        raw = (self.cleaned_data.get("company_number") or "").strip()
        return raw.replace(" ", "").upper()

    def clean(self):
        cleaned = super().clean()
        company_number = cleaned.get("company_number")
        should_validate = cleaned.get("validate_company_with_companies_house")
        self._companies_house_payload = None

        if company_number and should_validate:
            payload = CompaniesHouseService().get_company(company_number)
            if not payload:
                self.add_error(
                    "company_number",
                    "Company number could not be verified with Companies House. Check the number or API key.",
                )
            else:
                self._companies_house_payload = payload
        return cleaned

    def _build_registered_address(self, payload):
        address = (payload or {}).get("registered_office_address") or {}
        parts = [
            address.get("address_line_1", ""),
            address.get("address_line_2", ""),
            address.get("locality", ""),
            address.get("region", ""),
            address.get("postal_code", ""),
            address.get("country", ""),
        ]
        return "\n".join([p for p in parts if p]).strip()

    def _save_business_profile(self, user):
        company_name = (self.cleaned_data.get("company_name") or "").strip()
        company_number = (self.cleaned_data.get("company_number") or "").strip()
        payload = self._companies_house_payload

        existing = BusinessProfile.objects.filter(user=user).first()
        has_data = bool(company_name or company_number)
        if not has_data and not existing:
            return

        profile, _ = BusinessProfile.objects.get_or_create(
            user=user,
            defaults={"company_name": company_name or user.email},
        )

        if payload and not company_name:
            company_name = (payload.get("company_name") or "").strip() or company_name

        profile.company_name = company_name or profile.company_name or user.email
        profile.company_number = company_number
        profile.company_type = (payload or {}).get("type", "")
        profile.status = (payload or {}).get("company_status", "")
        profile.registered_address = self._build_registered_address(payload)
        profile.companies_house_data = payload or {}
        profile.is_verified = bool(payload)
        profile.verified_at = timezone.now() if payload else None
        profile.save()

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            self._save_business_profile(user)
        return user


class HomeFAQForm(forms.ModelForm):
    class Meta:
        model = HomeFAQ
        fields = ["question", "answer", "sort_order", "is_active"]


class HomeServiceCardForm(forms.ModelForm):
    class Meta:
        model = HomeServiceCard
        fields = [
            "title",
            "subtitle",
            "description",
            "icon_emoji",
            "cta_label",
            "cta_url",
            "features",
            "sort_order",
            "is_active",
        ]


class PackageCardForm(forms.ModelForm):
    class Meta:
        model = Package
        fields = [
            "name",
            "price_monthly",
            "price_annually",
            "setup_fee",
            "show_on_homepage",
            "card_blurb",
            "card_badge",
            "card_cta_label",
            "card_sort_order",
            "is_active",
            "is_featured",
        ]


class LegalPageForm(forms.ModelForm):
    class Meta:
        model = LegalPage
        fields = ["slug", "title", "summary", "content", "show_in_footer", "sort_order", "is_published"]


class ErrorPageContentForm(forms.ModelForm):
    class Meta:
        model = ErrorPageContent
        fields = ["status_code", "title", "subtitle", "body", "cta_label", "cta_url", "animation_style"]


class SiteContentSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteContentSettings
        fields = [
            "site_tagline",
            "footer_about",
            "support_email",
            "support_phone",
            "enable_cookie_banner",
            "cookie_banner_text",
            "cookie_policy_slug",
        ]


class DomainForm(forms.ModelForm):
    class Meta:
        model = Domain
        fields = [
            "user",
            "name",
            "tld",
            "status",
            "registrar_id",
            "registered_at",
            "expires_at",
            "auto_renew",
            "is_locked",
            "dns_provider",
            "cloudflare_zone_id",
            "nameserver1",
            "nameserver2",
            "nameserver3",
            "nameserver4",
            "epp_code",
        ]


class DomainContactAdminForm(forms.ModelForm):
    class Meta:
        model = DomainContact
        fields = [
            "user",
            "label",
            "name",
            "company",
            "company_number",
            "email",
            "phone_country_code",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postcode",
            "country",
            "is_default",
            "registrant_validation_status",
            "registrant_validated_at",
            "registrant_validation_notes",
            "registrar_contact_id",
        ]


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = [
            "user",
            "package",
            "status",
            "domain_name",
            "cpanel_username",
            "cpanel_domain",
            "cpanel_ip",
            "cpanel_server",
            "billing_period",
            "next_due_date",
            "notes",
        ]


class SupportTicketForm(forms.ModelForm):
    class Meta:
        model = SupportTicket
        fields = [
            "user",
            "department",
            "subject",
            "status",
            "priority",
            "assigned_to",
            "related_service",
            "related_domain",
        ]


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = [
            "user",
            "invoice",
            "provider",
            "status",
            "amount",
            "currency",
            "external_id",
            "provider_data",
            "notes",
            "refund_amount",
            "refunded_at",
        ]


class WebsiteTemplateForm(forms.ModelForm):
    class Meta:
        model = WebsiteTemplate
        fields = [
            "name",
            "slug",
            "category",
            "description",
            "zip_filename",
            "extracted_path",
            "has_index",
            "security_notes",
            "jquery_version",
            "bootstrap_version",
            "is_sanitised",
            "is_active",
        ]
