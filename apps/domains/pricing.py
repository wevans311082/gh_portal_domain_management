from decimal import Decimal

from django.utils import timezone

from apps.domains.models import DomainPricingSettings, TLDPricing
from apps.domains.resellerclub_client import ResellerClubClient


class TLDPricingService:
    PRICE_KEYS = (
        "selling_price",
        "customer_price",
        "price",
        "amount",
        "total",
        "subtotal",
    )

    def __init__(self, client=None):
        self.client = client or ResellerClubClient()

    def sync_pricing(self, tlds=None, years=1):
        settings_obj = DomainPricingSettings.get_solo()
        supported_tlds = tlds or settings_obj.supported_tlds
        synced_records = []
        synced_at = timezone.now()

        for tld in supported_tlds:
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

    def _find_price_value(self, payload):
        if isinstance(payload, (int, float, Decimal)):
            return payload

        if isinstance(payload, str):
            try:
                return Decimal(payload)
            except Exception:
                return None

        if isinstance(payload, dict):
            for key in self.PRICE_KEYS:
                if key in payload:
                    candidate = self._find_price_value(payload[key])
                    if candidate is not None:
                        return candidate
            for value in payload.values():
                candidate = self._find_price_value(value)
                if candidate is not None:
                    return candidate

        if isinstance(payload, (list, tuple)):
            for item in payload:
                candidate = self._find_price_value(item)
                if candidate is not None:
                    return candidate

        return None