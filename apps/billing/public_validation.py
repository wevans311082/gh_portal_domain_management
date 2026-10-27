"""Typed public quote input and bounded, server-priced catalogue lines."""
from decimal import Decimal

from django import forms

from apps.products.models import Package
from .models import BillingDocumentBranding
from .services import LineItemSpec


class PublicQuoteLeadForm(forms.Form):
    name = forms.CharField(max_length=200)
    email = forms.EmailField(max_length=254)
    company = forms.CharField(max_length=255, required=False)
    phone = forms.CharField(max_length=50, required=False)
    notes = forms.CharField(max_length=4000, required=False)


def validate_public_quote(payload):
    if set(payload) - {"lead", "items", "hp", "submission_key"}:
        raise forms.ValidationError("The quote contains unsupported fields.")
    lead = payload.get("lead")
    if (not isinstance(lead, dict) or set(lead) - set(PublicQuoteLeadForm.base_fields)
            or any(not isinstance(value, str) for value in lead.values())):
        raise forms.ValidationError("Enter valid contact details.")
    form = PublicQuoteLeadForm(lead)
    if not form.is_valid():
        raise forms.ValidationError("Enter valid contact details within the displayed field limits.")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise forms.ValidationError("Choose between 1 and 20 quote lines.")
    normalized = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {"description", "quantity", "package_id", "product_id", "unit_price", "kind"}:
            raise forms.ValidationError("A quote line has an invalid structure.")
        identifiers = [item[key] for key in ("package_id", "product_id") if key in item]
        if (not identifiers or any(type(value) not in {str, int} or not str(value).isascii()
                                  or not str(value).isdecimal() or len(str(value)) > 10 or int(value) < 1 for value in identifiers)
                or len({int(value) for value in identifiers}) != 1):
            raise forms.ValidationError("Choose an available catalogue item.")
        quantity = item.get("quantity", 1)
        if type(quantity) not in {int, str} or not str(quantity).isascii() or not str(quantity).isdecimal() or not 1 <= int(quantity) <= 100:
            raise forms.ValidationError("Each quantity must be a whole number from 1 to 100.")
        description = item.get("description", "")
        if not isinstance(description, str) or len(description) > 500 or any(ord(char) < 32 for char in description):
            raise forms.ValidationError("Quote descriptions must be single-line text of at most 500 characters.")
        kind = item.get("kind")
        if kind is not None and (not isinstance(kind, str) or kind not in {"monthly", "setup"}):
            raise forms.ValidationError("Choose a supported price type.")
        normalized.append((int(identifiers[0]), int(quantity), description.strip(), item.get("kind")))
    packages = Package.objects.filter(pk__in=[item[0] for item in normalized], is_active=True, is_quotable=True).in_bulk()
    result = []
    seen = set()
    subtotal = Decimal("0")
    for position, (identifier, quantity, description, kind) in enumerate(normalized):
        package = packages.get(identifier)
        if package is None:
            raise forms.ValidationError("A selected catalogue item is no longer available.")
        # Older clients sent the catalogue's exact setup description. Arbitrary
        # descriptions containing 'setup' must never select a cheaper price.
        kind = kind or ("setup" if description == package.name + " — setup fee" else "monthly")
        if (identifier, kind) in seen:
            raise forms.ValidationError("Combine repeated catalogue lines into one quantity.")
        seen.add((identifier, kind))
        price = package.setup_fee if kind == "setup" else package.price_monthly
        if not price.is_finite() or price < 0 or (kind == "setup" and (price <= 0 or quantity != 1)):
            raise forms.ValidationError("The selected catalogue price is unavailable.")
        subtotal += price * quantity
        result.append(LineItemSpec(description=description or package.name, quantity=Decimal(quantity), unit_price=price, position=position))
    vat = BillingDocumentBranding.get_solo().default_vat_rate
    if not vat.is_finite() or not 0 <= vat <= 100 or subtotal * (1 + vat / 100) > 100000:
        raise forms.ValidationError("The quote exceeds the supported public quote value. Contact us for a larger order.")
    return form.cleaned_data, result
