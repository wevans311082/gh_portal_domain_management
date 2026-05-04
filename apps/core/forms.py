from django import forms

_INPUT_CSS = "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus:outline-none focus:ring-2 focus:ring-sky-500 focus:border-sky-500"
_TEXTAREA_CSS = _INPUT_CSS + " resize-y"


class ContactForm(forms.Form):
    name = forms.CharField(
        max_length=200,
        label="Full name",
        widget=forms.TextInput(attrs={"class": _INPUT_CSS, "placeholder": "e.g. Jane Smith"}),
    )
    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(attrs={"class": _INPUT_CSS, "placeholder": "e.g. jane@example.com"}),
    )
    phone = forms.CharField(
        max_length=50,
        required=False,
        label="Phone (optional)",
        widget=forms.TextInput(attrs={"class": _INPUT_CSS, "placeholder": "e.g. 07700 900000"}),
    )
    subject = forms.CharField(
        max_length=255,
        required=False,
        label="Subject",
        widget=forms.TextInput(attrs={"class": _INPUT_CSS, "placeholder": "e.g. General enquiry"}),
    )
    message = forms.CharField(
        label="Message",
        widget=forms.Textarea(attrs={"class": _TEXTAREA_CSS, "rows": 5, "placeholder": "How can we help?"}),
    )
    # Honeypot — must remain empty; bots tend to fill every field
    website = forms.CharField(required=False, widget=forms.HiddenInput)
