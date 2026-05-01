"""Forms for the customer cPanel self-service portal."""
from django import forms

_INPUT = "block w-full rounded-md border-gray-300 shadow-sm focus:border-sky-500 focus:ring-sky-500 sm:text-sm"


class EmailAccountForm(forms.Form):
    email_user = forms.CharField(
        max_length=64,
        label="Mailbox name",
        help_text="The part before the @",
        widget=forms.TextInput(attrs={"class": _INPUT, "placeholder": "info"}),
    )
    password = forms.CharField(
        max_length=128,
        label="Password",
        widget=forms.PasswordInput(attrs={"class": _INPUT}),
    )
    password_confirm = forms.CharField(
        max_length=128,
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"class": _INPUT}),
    )
    quota_mb = forms.IntegerField(
        label="Quota (MB)",
        initial=500,
        min_value=0,
        max_value=10240,
        widget=forms.NumberInput(attrs={"class": _INPUT}),
        help_text="0 = unlimited",
    )

    def clean_email_user(self):
        val = self.cleaned_data["email_user"].strip().lower()
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-+")
        if not val:
            raise forms.ValidationError("Mailbox name is required.")
        if not all(c in allowed for c in val):
            raise forms.ValidationError("Only letters, numbers, and . _ - + are allowed.")
        return val

    def clean(self):
        cleaned = super().clean()
        pw = cleaned.get("password")
        confirm = cleaned.get("password_confirm")
        if pw and confirm and pw != confirm:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned


class DatabaseForm(forms.Form):
    db_name = forms.CharField(
        max_length=32,
        label="Database name",
        help_text="Alphanumeric and underscores only. Your cPanel username will be prefixed automatically.",
        widget=forms.TextInput(attrs={"class": _INPUT, "placeholder": "myapp_db"}),
    )

    def clean_db_name(self):
        val = self.cleaned_data["db_name"].strip().lower()
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_")
        if not val:
            raise forms.ValidationError("Database name is required.")
        if not all(c in allowed for c in val):
            raise forms.ValidationError("Only letters, numbers, and underscores are allowed.")
        return val
