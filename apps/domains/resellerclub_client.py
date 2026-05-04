"""ResellerClub (LogicBoxes) API client for domain management."""
import logging
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from apps.core.runtime_settings import get_runtime_setting
from apps.domains.debug_state import add_entry

logger = logging.getLogger(__name__)

# Connection/read timeouts (seconds)
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 30
_TLD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}(?:\.[a-z0-9][a-z0-9-]{0,62})*$")


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

    The LogicBoxes HTTP API authenticates via query-string parameters
    ``auth-userid`` (your Reseller ID) and ``api-key`` on every request.
    HTTP Basic Auth is NOT supported — using it causes JWT/token errors.

    reseller_id  — your ResellerClub *Reseller* account ID (used for auth)
    customer_id  — a customer account under your reseller (used for domain
                   registration; can be your own master customer account)
    """

    def __init__(self):
        self.reseller_id = get_runtime_setting("RESELLERCLUB_RESELLER_ID", "")
        self.api_key = get_runtime_setting("RESELLERCLUB_API_KEY", "")
        self.base_url = get_runtime_setting("RESELLERCLUB_API_URL", "https://httpapi.com/api").rstrip("/")
        self.session = _build_session()
        # LogicBoxes API requires these on EVERY request as query/form params
        self._auth_params = {
            "auth-userid": self.reseller_id,
            "api-key": self.api_key,
        }

    @staticmethod
    def _normalize_domain_labels(domain_names: list) -> list:
        labels = []
        for raw in (domain_names or []):
            value = str(raw or "").strip().lower()
            if not value:
                continue
            if "." in value:
                value = value.split(".", 1)[0]
            labels.append(value)
        return labels

    @staticmethod
    def _normalize_tlds(tlds: list) -> list:
        normalized = []
        for raw in (tlds or []):
            value = str(raw or "").strip().lower().lstrip(".")
            if value:
                normalized.append(value)
        return normalized

    def _check_response(self, data: dict, endpoint: str) -> dict:
        """Raise ResellerClubError when the API returns a business-level error."""
        if isinstance(data, dict) and data.get("status") == "ERROR":
            error = data.get("message") or data.get("error") or "Unknown error"
            # Detect auth errors explicitly so callers get a clear message
            error_lower = str(error).lower()
            if any(kw in error_lower for kw in ("jwt", "token", "auth", "invalid key", "unauthorized")):
                logger.error(f"ResellerClub auth failure at {endpoint}: {error}")
                raise ResellerClubError(
                    f"ResellerClub authentication failed — check Reseller ID and API key: {error}"
                )
            logger.error(f"ResellerClub API error at {endpoint}: {error}")
            raise ResellerClubError(f"ResellerClub error: {error}")
        return data

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        """LogicBoxes endpoints are expected to be called with .json suffix."""
        cleaned = (endpoint or "").strip().lstrip("/")
        if not cleaned.endswith(".json"):
            cleaned = f"{cleaned}.json"
        return cleaned

    def _capture_debug(self, request_data: dict, response_data: dict = None, error: str = ""):
        debug_mode = str(get_runtime_setting("RESELLERCLUB_DEBUG_MODE", "false")).strip().lower() in (
            "1", "true", "yes", "on"
        )
        if not debug_mode:
            return
        add_entry(
            {
                "request": request_data,
                "response": response_data,
                "error": error,
            }
        )

    def _get(self, endpoint: str, params: dict = None) -> dict:
        """Make an authenticated GET request to the ResellerClub API."""
        normalized_endpoint = self._normalize_endpoint(endpoint)
        url = f"{self.base_url}/{normalized_endpoint}"
        merged_params = {**self._auth_params, **(params or {})}
        try:
            response = self.session.get(
                url,
                params=merged_params,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            self._capture_debug(
                request_data={
                    "method": "GET",
                    "url": response.request.url,
                    "headers": dict(response.request.headers),
                    "body": response.request.body.decode("utf-8", errors="replace")
                    if isinstance(response.request.body, bytes)
                    else (response.request.body or ""),
                    "params": merged_params,
                    "endpoint": normalized_endpoint,
                },
                response_data={
                    "status_code": response.status_code,
                    "reason": response.reason,
                    "headers": dict(response.headers),
                    "text": response.text,
                },
            )
            response.raise_for_status()
            data = response.json()
        except requests.HTTPError as e:
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            body = (getattr(getattr(e, "response", None), "text", "") or "")[:300]
            resp = getattr(e, "response", None)
            req = getattr(resp, "request", None)
            self._capture_debug(
                request_data={
                    "method": getattr(req, "method", "GET"),
                    "url": getattr(req, "url", url),
                    "headers": dict(getattr(req, "headers", {}) or {}),
                    "body": (
                        getattr(req, "body", b"").decode("utf-8", errors="replace")
                        if isinstance(getattr(req, "body", None), bytes)
                        else (getattr(req, "body", "") or "")
                    ),
                    "params": merged_params,
                    "endpoint": normalized_endpoint,
                },
                response_data={
                    "status_code": getattr(resp, "status_code", None),
                    "reason": getattr(resp, "reason", ""),
                    "headers": dict(getattr(resp, "headers", {}) or {}),
                    "text": getattr(resp, "text", ""),
                },
                error=str(e),
            )
            if status_code and status_code >= 500:
                logger.error("ResellerClub GET %s server error %s: %s", normalized_endpoint, status_code, body)
                raise ResellerClubError(
                    "ResellerClub returned a server error. This commonly happens when request parameters "
                    "are malformed (for example domain-name should be label only)."
                ) from e
            logger.error(f"ResellerClub GET {normalized_endpoint} failed: {e}")
            raise ResellerClubError(f"API request failed: {e}") from e
        except requests.RequestException as e:
            self._capture_debug(
                request_data={
                    "method": "GET",
                    "url": url,
                    "headers": {},
                    "body": "",
                    "params": merged_params,
                    "endpoint": normalized_endpoint,
                },
                response_data=None,
                error=str(e),
            )
            logger.error(f"ResellerClub GET {normalized_endpoint} failed: {e}")
            raise ResellerClubError(f"API request failed: {e}") from e
        return self._check_response(data, normalized_endpoint)

    def _post(self, endpoint: str, data: dict = None) -> dict:
        """Make an authenticated POST request to the ResellerClub API."""
        normalized_endpoint = self._normalize_endpoint(endpoint)
        url = f"{self.base_url}/{normalized_endpoint}"
        merged_data = {**self._auth_params, **(data or {})}
        try:
            response = self.session.post(
                url,
                data=merged_data,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            self._capture_debug(
                request_data={
                    "method": "POST",
                    "url": response.request.url,
                    "headers": dict(response.request.headers),
                    "body": response.request.body.decode("utf-8", errors="replace")
                    if isinstance(response.request.body, bytes)
                    else (response.request.body or ""),
                    "data": merged_data,
                    "endpoint": normalized_endpoint,
                },
                response_data={
                    "status_code": response.status_code,
                    "reason": response.reason,
                    "headers": dict(response.headers),
                    "text": response.text,
                },
            )
            response.raise_for_status()
            result = response.json()
        except requests.HTTPError as e:
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            body = (getattr(getattr(e, "response", None), "text", "") or "")[:300]
            resp = getattr(e, "response", None)
            req = getattr(resp, "request", None)
            self._capture_debug(
                request_data={
                    "method": getattr(req, "method", "POST"),
                    "url": getattr(req, "url", url),
                    "headers": dict(getattr(req, "headers", {}) or {}),
                    "body": (
                        getattr(req, "body", b"").decode("utf-8", errors="replace")
                        if isinstance(getattr(req, "body", None), bytes)
                        else (getattr(req, "body", "") or "")
                    ),
                    "data": merged_data,
                    "endpoint": normalized_endpoint,
                },
                response_data={
                    "status_code": getattr(resp, "status_code", None),
                    "reason": getattr(resp, "reason", ""),
                    "headers": dict(getattr(resp, "headers", {}) or {}),
                    "text": getattr(resp, "text", ""),
                },
                error=str(e),
            )
            if status_code and status_code >= 500:
                logger.error("ResellerClub POST %s server error %s: %s", normalized_endpoint, status_code, body)
                raise ResellerClubError("ResellerClub returned a server error while processing the request.") from e
            logger.error(f"ResellerClub POST {normalized_endpoint} failed: {e}")
            raise ResellerClubError(f"API POST failed: {e}") from e
        except requests.RequestException as e:
            self._capture_debug(
                request_data={
                    "method": "POST",
                    "url": url,
                    "headers": {},
                    "body": "",
                    "data": merged_data,
                    "endpoint": normalized_endpoint,
                },
                response_data=None,
                error=str(e),
            )
            logger.error(f"ResellerClub POST {normalized_endpoint} failed: {e}")
            raise ResellerClubError(f"API POST failed: {e}") from e
        return self._check_response(result, normalized_endpoint)

    def check_availability(self, domain_names: list, tlds: list) -> dict:
        """
        Check availability of domain names across TLDs.
        Returns dict of domain -> availability status.
        """
        labels = self._normalize_domain_labels(domain_names)
        normalized_tlds = self._normalize_tlds(tlds)
        if not labels or not normalized_tlds:
            raise ResellerClubError("Domain availability check requires at least one domain label and one TLD.")

        params = {
            "domain-name": ",".join(labels),
            "tlds": ",".join(normalized_tlds),
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

    @staticmethod
    def _normalize_tld_value(value: str) -> str:
        normalized = str(value or "").strip().lower().lstrip(".")
        if not normalized:
            return ""
        if normalized.endswith("-domain"):
            normalized = normalized[: -len("-domain")]
        if not _TLD_RE.match(normalized):
            return ""
        if normalized.isdigit():
            return ""
        return normalized

    def _extract_tlds_from_payload(self, payload) -> list:
        seen = set()

        def walk(node, hinted=False):
            if isinstance(node, dict):
                for key, value in node.items():
                    key_str = str(key or "").lower()
                    key_hinted = hinted or ("tld" in key_str) or ("extension" in key_str)

                    if key_str in {"productkey", "product-key", "product_key"} and isinstance(value, str):
                        product_value = str(value or "").strip().lower()
                        if product_value.endswith("-domain"):
                            tld = self._normalize_tld_value(product_value)
                            if tld:
                                seen.add(tld)
                        continue

                    # Some endpoints expose product keys like "com-domain"
                    if key_str.endswith("-domain"):
                        tld = self._normalize_tld_value(key_str)
                        if tld:
                            seen.add(tld)

                    walk(value, key_hinted)
                return

            if isinstance(node, (list, tuple, set)):
                for item in node:
                    walk(item, hinted)
                return

            if isinstance(node, str):
                # Product catalog payloads often provide TLDs as values such as
                # "com-domain" under keys like "productkey".
                value = str(node or "").strip().lower()
                if hinted or value.endswith("-domain"):
                    tld = self._normalize_tld_value(value)
                    if tld:
                        seen.add(tld)

        walk(payload)
        return sorted(seen)

    def list_available_tlds(self) -> list:
        """Return an imported TLD list by probing common LogicBoxes catalog endpoints."""
        candidates = [
            ("products/list", {"type": "domain"}),
            ("domains/available-tlds", {}),
            ("domains/tlds", {}),
        ]
        last_error = None

        for endpoint, params in candidates:
            try:
                payload = self._get(endpoint, params)
                tlds = self._extract_tlds_from_payload(payload)
                if tlds:
                    return tlds
            except Exception as exc:
                last_error = exc
                continue

        if last_error:
            raise ResellerClubError(f"Unable to import TLD list from registrar: {last_error}")
        raise ResellerClubError("Unable to import TLD list from registrar.")

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
