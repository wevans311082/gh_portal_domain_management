from decimal import Decimal

from django.utils import timezone

from apps.domains.models import DomainPricingSettings, TLDPricing
from apps.domains.resellerclub_client import ResellerClubClient


class TLDPricingService:
    PRICE_KEYS = (
        "customer-currency-amount",
        "selling-currency-amount",
        "reseller-currency-amount",
        "customer_currency_amount",
        "selling_currency_amount",
        "reseller_currency_amount",
        "sellingCurrencyAmount",
        "customerCurrencyAmount",
        "resellerCurrencyAmount",
        "customer-price",
        "selling-price",
        "reseller-price",
        "sellingPrice",
        "customerPrice",
        "resellerPrice",
        "selling_price",
        "customer_price",
        "reseller_price",
        "price",
        "price_value",
        "value",
        "amount_inr",
        "amount_usd",
        "amount_gbp",
        "amount",
        "amount_value",
        "total",
        "subtotal",
    )

    PRICE_HINT_TOKENS = (
        "price",
        "amount",
        "cost",
        "total",
        "fee",
        "sell",
        "customer",
        "reseller",
        "currency",
    )

    def __init__(self, client=None):
        self.client = client or ResellerClubClient()

    def sync_pricing(self, tlds=None, years=1):
        settings_obj = DomainPricingSettings.get_solo()
        supported_tlds = tlds or settings_obj.supported_tlds
        synced_records = []
        synced_at = timezone.now()
        errors = []

        normalized_tlds = []
        for tld in supported_tlds:
            cleaned = str(tld or "").strip().lower().lstrip(".")
            if cleaned:
                normalized_tlds.append(cleaned)

        # Prime the client's pricing/classkey cache in 2 API calls total
        # (1 chunked availability sweep + 1 catalog fetch) instead of 3 calls
        # per TLD.  Tests using a mock client without prime_pricing_cache are
        # silently skipped.
        prime = getattr(self.client, "prime_pricing_cache", None)
        if callable(prime):
            try:
                prime(normalized_tlds)
            except Exception as exc:
                errors.append(f"prime: {exc}")

        for tld in normalized_tlds:
            try:
                payload = self.client.get_tld_costs(tld=tld, years=years)
                pricing, _ = TLDPricing.objects.update_or_create(
                    tld=tld,
                    defaults={
                        "registration_cost": self._extract_amount(payload.get("registration", {})),
                        "renewal_cost": self._extract_amount(payload.get("renewal", {})),
                        "transfer_cost": self._extract_amount(payload.get("transfer", {})),
                        "last_synced_at": synced_at,
                        "last_sync_payload": self._json_safe(payload),
                        "is_active": True,
                    },
                )
                synced_records.append(pricing)
            except Exception as exc:
                errors.append(f".{tld}: {exc}")

        if errors:
            settings_obj.last_sync_error = " | ".join(errors[:10])
            settings_obj.save(update_fields=["last_sync_error", "updated_at"])

        return synced_records

    def _extract_amount(self, payload):
        value = self._find_price_value(payload)
        if value is None:
            return Decimal("0.00")
        return Decimal(str(value)).quantize(Decimal("0.01"))

    def _json_safe(self, payload):
        if isinstance(payload, Decimal):
            return str(payload)
        if isinstance(payload, dict):
            return {key: self._json_safe(value) for key, value in payload.items()}
        if isinstance(payload, (list, tuple)):
            return [self._json_safe(item) for item in payload]
        return payload

    @staticmethod
    def _normalize_decimal_string(value: str):
        cleaned = str(value or "").strip()
        if not cleaned:
            return ""
        # Remove common currency symbols/codes while preserving digits and sign.
        cleaned = cleaned.replace(",", "")
        cleaned = cleaned.replace("GBP", "").replace("USD", "").replace("EUR", "")
        cleaned = cleaned.replace("£", "").replace("$", "").replace("€", "")
        return cleaned.strip()

    def _find_price_value(self, payload, hinted=False):
        if isinstance(payload, bool):
            return None

        if isinstance(payload, Decimal):
            return payload

        if isinstance(payload, (int, float)):
            # Only accept primitive numerics when we are in a price-like context.
            return payload if hinted else None

        if isinstance(payload, str):
            try:
                cleaned = self._normalize_decimal_string(payload)
                if not cleaned:
                    return None
                return Decimal(cleaned)
            except Exception:
                return None

        if isinstance(payload, dict):
            lowered = {str(k).lower(): k for k in payload.keys()}

            # Strong preference for explicit known pricing keys.
            for key in self.PRICE_KEYS:
                source_key = lowered.get(key.lower())
                if source_key is not None:
                    candidate = self._find_price_value(payload[source_key], hinted=True)
                    if candidate is not None:
                        return candidate

            # Recursive fallback, but only treat primitive values as prices when key name hints price context.
            for raw_key, value in payload.items():
                key_text = str(raw_key or "").lower()
                child_hinted = hinted or any(token in key_text for token in self.PRICE_HINT_TOKENS)
                candidate = self._find_price_value(value, hinted=child_hinted)
                if candidate is not None:
                    return candidate

        if isinstance(payload, (list, tuple)):
            for item in payload:
                # Lists usually carry priced rows; preserve current hint.
                candidate = self._find_price_value(item, hinted=hinted)
                if candidate is not None:
                    return candidate

        return None