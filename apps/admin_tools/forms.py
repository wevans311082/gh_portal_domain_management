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
