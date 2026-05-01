from django import forms
from .models import DNSRecord


class DNSRecordForm(forms.ModelForm):
    class Meta:
        model = DNSRecord
        fields = ["record_type", "name", "content", "ttl", "priority", "proxied"]
        widgets = {
            "record_type": forms.Select(attrs={"class": "block w-full rounded-md border-gray-300 shadow-sm focus:border-sky-500 focus:ring-sky-500 sm:text-sm"}),
            "name": forms.TextInput(attrs={"class": "block w-full rounded-md border-gray-300 shadow-sm focus:border-sky-500 focus:ring-sky-500 sm:text-sm", "placeholder": "@ or subdomain"}),
            "content": forms.TextInput(attrs={"class": "block w-full rounded-md border-gray-300 shadow-sm focus:border-sky-500 focus:ring-sky-500 sm:text-sm", "placeholder": "IP address or value"}),
            "ttl": forms.NumberInput(attrs={"class": "block w-full rounded-md border-gray-300 shadow-sm focus:border-sky-500 focus:ring-sky-500 sm:text-sm"}),
            "priority": forms.NumberInput(attrs={"class": "block w-full rounded-md border-gray-300 shadow-sm focus:border-sky-500 focus:ring-sky-500 sm:text-sm", "placeholder": "MX/SRV only"}),
            "proxied": forms.CheckboxInput(attrs={"class": "rounded border-gray-300 text-sky-600 focus:ring-sky-500"}),
        }
        labels = {
            "record_type": "Type",
            "name": "Name / Host",
            "content": "Value / Content",
            "ttl": "TTL (seconds)",
            "priority": "Priority",
            "proxied": "Proxied (Cloudflare only)",
        }

    def clean(self):
        cleaned = super().clean()
        record_type = cleaned.get("record_type")
        priority = cleaned.get("priority")
        if record_type in (DNSRecord.TYPE_MX, DNSRecord.TYPE_SRV) and priority is None:
            self.add_error("priority", "Priority is required for MX and SRV records.")
        return cleaned
