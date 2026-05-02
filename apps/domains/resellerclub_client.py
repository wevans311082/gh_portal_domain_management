"""ResellerClub (LogicBoxes) API client for domain management."""
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from apps.core.runtime_settings import get_runtime_setting

logger = logging.getLogger(__name__)

# Connection/read timeouts (seconds)
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 30


class ResellerClubError(Exception):
    """Raised when the ResellerClub API returns an error."""
    pass


def _build_session() -> requests.Session:
    """Build a requests Session with retry logic and sensible timeouts."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class ResellerClubClient:
    """
    HTTP API client for the ResellerClub/LogicBoxes domain registrar API.

    Credentials are transmitted via HTTP Basic Auth (Authorization header)
    rather than as query-string parameters to prevent credential leakage
    in server logs, browser history, CDN caches, and Referer headers.
    """

    def __init__(self):
        self.reseller_id = get_runtime_setting("RESELLERCLUB_RESELLER_ID", "")
        self.api_key = get_runtime_setting("RESELLERCLUB_API_KEY", "")
        self.base_url = get_runtime_setting("RESELLERCLUB_API_URL", "https://test.httpapi.com/api").rstrip("/")
        self.session = _build_session()
        # Credentials sent via Basic Auth header — never in the URL
        self.session.auth = (self.reseller_id, self.api_key)

    def _check_response(self, data: dict, endpoint: str) -> dict:
        """Raise ResellerClubError when the API returns a business-level error."""
        if isinstance(data, dict) and data.get("status") == "ERROR":
            error = data.get("message") or data.get("error") or "Unknown error"
            logger.error(f"ResellerClub API error at {endpoint}: {error}")
            raise ResellerClubError(f"ResellerClub error: {error}")
        return data

    def _get(self, endpoint: str, params: dict = None) -> dict:
        """Make an authenticated GET request to the ResellerClub API."""
        url = f"{self.base_url}/{endpoint}"
        try:
            response = self.session.get(
                url,
                params=params or {},
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.error(f"ResellerClub GET {endpoint} failed: {e}")
            raise ResellerClubError(f"API request failed: {e}") from e
        return self._check_response(data, endpoint)

    def _post(self, endpoint: str, data: dict = None) -> dict:
        """Make an authenticated POST request to the ResellerClub API."""
        url = f"{self.base_url}/{endpoint}"
        try:
            response = self.session.post(
                url,
                data=data or {},
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as e:
            logger.error(f"ResellerClub POST {endpoint} failed: {e}")
            raise ResellerClubError(f"API POST failed: {e}") from e
        return self._check_response(result, endpoint)

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

    def get_tld_pricing(self, tld: str, years: int = 1, action: str = "registration") -> dict:
        """Get pricing metadata for a TLD without needing a specific domain name."""
        return self.get_price(domain_name=f"example.{tld}", tld=tld, action=action, years=years)

    def get_tld_costs(self, tld: str, years: int = 1) -> dict:
        """Return registration, renewal, and transfer pricing payloads for a TLD."""
        return {
            "registration": self.get_tld_pricing(tld=tld, years=years, action="registration"),
            "renewal": self.get_tld_pricing(tld=tld, years=years, action="renewal"),
            "transfer": self.get_tld_pricing(tld=tld, years=years, action="transfer"),
        }

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

    def create_contact(self, payload: dict) -> dict:
        """Create a domain contact in ResellerClub."""
        return self._post("contacts/add", payload)

    def update_contact(self, contact_id: str, payload: dict) -> dict:
        """Update an existing domain contact in ResellerClub."""
        data = {"contact-id": contact_id, **payload}
        return self._post("contacts/modify", data)

    def get_contact(self, contact_id: str) -> dict:
        """Fetch a single domain contact from ResellerClub."""
        return self._get("contacts/details", {"contact-id": contact_id})
