"""ResellerClub (LogicBoxes) API client for domain management."""
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class ResellerClubError(Exception):
    """Raised when the ResellerClub API returns an error."""
    pass


class ResellerClubClient:
    """HTTP API client for the ResellerClub/LogicBoxes domain registrar API."""

    def __init__(self):
        self.reseller_id = settings.RESELLERCLUB_RESELLER_ID
        self.api_key = settings.RESELLERCLUB_API_KEY
        self.base_url = settings.RESELLERCLUB_API_URL
        self.session = requests.Session()

    def _get(self, endpoint: str, params: dict = None) -> dict:
        """Make a GET request to the ResellerClub API."""
        params = params or {}
        params["auth-userid"] = self.reseller_id
        params["api-key"] = self.api_key

        url = f"{self.base_url}/{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.error(f"ResellerClub API request failed: {e}")
            raise ResellerClubError(f"API request failed: {e}") from e

        if isinstance(data, dict) and data.get("status") == "ERROR":
            error = data.get("message", data.get("error", "Unknown error"))
            logger.error(f"ResellerClub API error at {endpoint}: {error}")
            raise ResellerClubError(f"ResellerClub error: {error}")

        return data

    def _post(self, endpoint: str, data: dict = None) -> dict:
        """Make a POST request to the ResellerClub API."""
        data = data or {}
        data["auth-userid"] = self.reseller_id
        data["api-key"] = self.api_key

        url = f"{self.base_url}/{endpoint}"
        try:
            response = self.session.post(url, data=data, timeout=30)
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as e:
            logger.error(f"ResellerClub API POST failed: {e}")
            raise ResellerClubError(f"API POST failed: {e}") from e

        if isinstance(result, dict) and result.get("status") == "ERROR":
            error = result.get("message", result.get("error", "Unknown error"))
            raise ResellerClubError(f"ResellerClub error: {error}")

        return result

    def check_availability(self, domain_names: list, tlds: list) -> dict:
        """
        Check availability of domain names across TLDs.
        Returns dict of domain -> availability status.
        """
        params = {
            "domain-name": domain_names,
            "tlds": tlds,
        }
        return self._get("domains/available", params)

    def suggest_names(self, keyword: str, tlds: list) -> list:
        """Get domain name suggestions based on a keyword."""
        params = {
            "keyword": keyword,
            "tlds": tlds,
            "hyphen-allowed": False,
            "add-related": True,
        }
        return self._get("domains/suggest-names", params)

    def get_price(self, domain_name: str, tld: str, action: str = "registration", years: int = 1) -> dict:
        """Get pricing for a domain action (registration/renewal/transfer)."""
        params = {
            "action": action,
            "productkey": f"{tld}-domain",
            "years": years,
        }
        return self._get("products/customer-price", params)

    def register_domain(
        self,
        domain_name: str,
        years: int,
        customer_id: str,
        reg_contact_id: str,
        admin_contact_id: str,
        tech_contact_id: str,
        billing_contact_id: str,
        nameservers: list,
        purchase_privacy: bool = True,
        auto_renew: bool = True,
    ) -> dict:
        """Register a domain name."""
        data = {
            "domain-name": domain_name,
            "years": years,
            "ns": nameservers,
            "customer-id": customer_id,
            "reg-contact-id": reg_contact_id,
            "admin-contact-id": admin_contact_id,
            "tech-contact-id": tech_contact_id,
            "billing-contact-id": billing_contact_id,
            "purchase-privacy": purchase_privacy,
            "auto-renew": auto_renew,
        }
        return self._post("domains/register", data)

    def renew_domain(self, order_id: str, years: int, current_expiry_date: int, auto_renew: bool = True) -> dict:
        """Renew a domain name."""
        data = {
            "order-id": order_id,
            "years": years,
            "exp-date": current_expiry_date,
            "auto-renew": auto_renew,
        }
        return self._post("domains/renew", data)

    def get_order_details(self, order_id: str) -> dict:
        """Get details for a domain order."""
        return self._get("domains/details", {"order-id": order_id, "options": "All"})

    def modify_nameservers(self, order_id: str, nameservers: list) -> dict:
        """Update the nameservers for a domain."""
        data = {
            "order-id": order_id,
            "ns": nameservers,
        }
        return self._post("domains/modify-ns", data)

    def lock_domain(self, order_id: str) -> dict:
        """Enable registrar lock on a domain."""
        return self._post("domains/enable-theft-protection", {"order-id": order_id})

    def unlock_domain(self, order_id: str) -> dict:
        """Disable registrar lock on a domain."""
        return self._post("domains/disable-theft-protection", {"order-id": order_id})

    def get_auth_code(self, order_id: str) -> dict:
        """Get the EPP/auth code for domain transfer out."""
        return self._get("domains/auth-code", {"order-id": order_id})

    def add_dns_record(self, order_id: str, host: str, value: str, record_type: str, ttl: int = 3600) -> dict:
        """Add a DNS record via ResellerClub DNS."""
        data = {
            "order-id": order_id,
            "host": host,
            "value": value,
            "type": record_type,
            "ttl": ttl,
        }
        return self._post("dns/manage/add-record", data)

    def delete_dns_record(self, order_id: str, host: str, value: str, record_type: str) -> dict:
        """Delete a DNS record via ResellerClub DNS."""
        data = {
            "order-id": order_id,
            "host": host,
            "value": value,
            "type": record_type,
        }
        return self._post("dns/manage/delete-record", data)
