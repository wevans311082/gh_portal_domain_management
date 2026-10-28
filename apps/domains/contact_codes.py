"""Offline assigned-code datasets, pinned with the application dependencies.

pycountry supplies ISO 3166-1 assignments; phonenumbers supplies calling codes
from libphonenumber metadata. Membership proves neither identity nor reachability.
"""

from django import forms
import unicodedata
import phonenumbers
import pycountry


COUNTRY_CHOICES = tuple(sorted(
    ((country.alpha_2, f"{getattr(country, 'common_name', country.name)} ({country.alpha_2})")
     for country in pycountry.countries),
    key=lambda item: "".join(char for char in unicodedata.normalize("NFKD", item[1]) if not unicodedata.combining(char)).casefold(),
))
COUNTRY_CODES = frozenset(code for code, _ in COUNTRY_CHOICES)
CALLING_CODES = frozenset(str(code) for code in phonenumbers.supported_calling_codes())


class CountryChoiceField(forms.ChoiceField):
    def __init__(self, **kwargs):
        super().__init__(choices=(("", "Choose a country or territory"), *COUNTRY_CHOICES),
                         error_messages={"invalid_choice": "Choose an assigned ISO country or territory."}, **kwargs)

    def to_python(self, value):
        return super().to_python(value).strip().upper()

    def prepare_value(self, value):
        return self.to_python(value)
