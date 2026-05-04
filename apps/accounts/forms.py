import re

from django import forms
from django.contrib.auth.password_validation import validate_password

from .models import User, ClientProfile

# E.164-compatible phone pattern (international or UK local)
_PHONE_RE = re.compile(r"^\+?[\d\s\-\(\)]{7,20}$")


class RegistrationForm(forms.ModelForm):
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm Password", widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "phone"]

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email address already exists.")
        return email

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "").strip()
        if phone and not _PHONE_RE.match(phone):
            raise forms.ValidationError(
                "Enter a valid phone number (e.g. +44 7700 900123 or 07700 900123)."
            )
        return phone

    def clean_password1(self):
        password = self.cleaned_data.get("password1")
        if password:
            # Runs all configured AUTH_PASSWORD_VALIDATORS
            validate_password(password)
        return password

    def clean_password2(self):
        p1 = self.cleaned_data.get("password1")
        p2 = self.cleaned_data.get("password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("Passwords don't match.")
        return p2

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


class ProfileUpdateForm(forms.ModelForm):
    class Meta:
        model = ClientProfile
        fields = ["address_line1", "address_line2", "city", "county", "postcode", "country", "vat_number"]

    def clean_postcode(self):
        postcode = self.cleaned_data.get("postcode", "").strip().upper()
        return postcode


class TOTPVerifyForm(forms.Form):
    token = forms.CharField(
        label="Authenticator code",
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            "autocomplete": "one-time-code",
            "inputmode": "numeric",
            "pattern": "[0-9]{6}",
            "placeholder": "000000",
        }),
    )

    def clean_token(self):
        token = self.cleaned_data.get("token", "").strip()
        if not token.isdigit():
            raise forms.ValidationError("Enter the 6-digit code from your authenticator app.")
        return token


class MFALoginVerifyForm(forms.Form):
    code = forms.CharField(
        label="Authentication code or backup code",
        max_length=16,
        widget=forms.TextInput(attrs={
            "autocomplete": "one-time-code",
            "placeholder": "000000 or ABCD-EFGH",
        }),
    )

    def clean_code(self):
        value = (self.cleaned_data.get("code") or "").strip().upper()
        compact = value.replace(" ", "")
        if not compact:
            raise forms.ValidationError("Enter your authenticator code or backup code.")
        return value


class MFARegenerateBackupCodesForm(forms.Form):
    token = forms.CharField(
        label="Current authenticator code",
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            "autocomplete": "one-time-code",
            "inputmode": "numeric",
            "pattern": "[0-9]{6}",
            "placeholder": "000000",
        }),
    )

    def clean_token(self):
        token = (self.cleaned_data.get("token") or "").strip()
        if not token.isdigit():
            raise forms.ValidationError("Enter the 6-digit code from your authenticator app.")
        return token
