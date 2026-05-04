from django import forms
from django.contrib.auth.password_validation import validate_password

from apps.accounts.models import User
from apps.core.models import (
    ErrorPageContent,
    HomeFAQ,
    HomeServiceCard,
    LegalPage,
    SiteContentSettings,
)
from apps.products.models import Package
from apps.domains.models import Domain
from apps.services.models import Service
from apps.support.models import SupportTicket
from apps.payments.models import Payment
from apps.website_templates.models import WebsiteTemplate


class AdminUserCreateForm(forms.ModelForm):
    password1 = forms.CharField(widget=forms.PasswordInput)
    password2 = forms.CharField(widget=forms.PasswordInput)

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

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


class AdminUserUpdateForm(forms.ModelForm):
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
