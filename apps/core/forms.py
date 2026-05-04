from django import forms


class ContactForm(forms.Form):
    name = forms.CharField(max_length=200, label="Full name")
    email = forms.EmailField(label="Email address")
    phone = forms.CharField(max_length=50, required=False, label="Phone (optional)")
    subject = forms.CharField(max_length=255, required=False, label="Subject")
    message = forms.CharField(widget=forms.Textarea, label="Message")
    # Honeypot — must remain empty; bots tend to fill every field
    website = forms.CharField(required=False, widget=forms.HiddenInput)
