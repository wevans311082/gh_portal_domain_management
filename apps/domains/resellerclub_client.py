"""ResellerClub (LogicBoxes) API client for domain management."""
import logging
import re
from datetime import datetime, timezone
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
    """Build a requests Session with retry logic and sensible timeouts.

    Important: do **not** auto-retry POST/mutations on HTTP 500. ResellerClub
    often returns 500 for bad contact payloads; urllib3 retries then surface as
    ``too many 500 error responses`` and hammer the API.
    """
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.5,
        # Omit 500 — treat upstream application errors as final.
        status_forcelist=[429, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
        raise_on_status=False,
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
        self.reseller_id = self._clean_secret(get_runtime_setting("RESELLERCLUB_RESELLER_ID", ""))
        self.api_key = self._clean_secret(get_runtime_setting("RESELLERCLUB_API_KEY", ""))
        self.base_url = self._clean_secret(
            get_runtime_setting("RESELLERCLUB_API_URL", "https://httpapi.com/api")
        ).rstrip("/")
        if not self.base_url:
            self.base_url = "https://httpapi.com/api"
        self.session = _build_session()
        # LogicBoxes API requires these on EVERY request as query/form params
        self._auth_params = {
            "auth-userid": self.reseller_id,
            "api-key": self.api_key,
        }
        # Lazy caches populated on first pricing/classkey lookup
        self._pricing_catalog = None  # full customer-price.json catalog
        self._tld_classkeys = {}      # tld -> classkey mapping
        if not self.reseller_id or not self.api_key:
            logger.error(
                "ResellerClub credentials missing (reseller_id=%s, api_key_set=%s). "
                "Set RESELLERCLUB_RESELLER_ID and RESELLERCLUB_API_KEY in .env or Admin → Integrations.",
                bool(self.reseller_id),
                bool(self.api_key),
            )

    @staticmethod
    def _clean_secret(value) -> str:
        """Strip whitespace and accidental surrounding quotes from env/DB values."""
        text = str(value or "").strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1].strip()
        return text

    def ensure_configured(self):
        """Raise a clear error before making API calls without credentials."""
        if not self.reseller_id or not self.api_key:
            raise ResellerClubError(
                "ResellerClub is not configured. Set Reseller ID and API key under "
                "Admin Tools → Integrations (or RESELLERCLUB_* in .env)."
            )

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

    @staticmethod
    def _redact_secrets(value):
        secret_keys = {"api-key", "api_key", "auth-userid", "auth_userid", "password"}
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                if str(key).lower() in secret_keys:
                    redacted[key] = "[redacted]"
                else:
                    redacted[key] = ResellerClubClient._redact_secrets(item)
            return redacted
        if isinstance(value, list):
            return [ResellerClubClient._redact_secrets(item) for item in value]
        return value

    def _capture_debug(self, request_data: dict, response_data: dict = None, error: str = ""):
        debug_mode = str(get_runtime_setting("RESELLERCLUB_DEBUG_MODE", "false")).strip().lower() in (
            "1", "true", "yes", "on"
        )
        if not debug_mode:
            return
        safe_request = dict(request_data or {})
        safe_request["params"] = self._redact_secrets(safe_request.get("params") or {})
        url = str(safe_request.get("url") or "")
        if self.api_key:
            url = url.replace(self.api_key, "[redacted]")
        if self.reseller_id:
            url = url.replace(str(self.reseller_id), "[redacted]")
        safe_request["url"] = url
        add_entry(
            {
                "request": safe_request,
                "response": response_data,
                "error": error,
            }
        )

    def _get(self, endpoint: str, params: dict = None) -> dict:
        """Make an authenticated GET request to the ResellerClub API."""
        self.ensure_configured()
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
        self.ensure_configured()
        normalized_endpoint = self._normalize_endpoint(endpoint)
        url = f"{self.base_url}/{normalized_endpoint}"
        merged_data = {**self._auth_params, **(data or {})}
        # Ensure all values are strings/ints suitable for form encoding
        safe_data = {}
        for key, value in merged_data.items():
            if value is None:
                continue
            if isinstance(value, bool):
                safe_data[key] = "true" if value else "false"
            elif isinstance(value, (list, tuple)):
                # LogicBoxes often wants repeated keys; requests handles list values
                safe_data[key] = list(value)
            else:
                safe_data[key] = value
        try:
            response = self.session.post(
                url,
                data=safe_data,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            body_text = response.text or ""
            self._capture_debug(
                request_data={
                    "method": "POST",
                    "url": response.request.url,
                    "headers": dict(response.request.headers),
                    "body": response.request.body.decode("utf-8", errors="replace")
                    if isinstance(response.request.body, bytes)
                    else (response.request.body or ""),
                    "data": {k: ("***" if k == "api-key" else v) for k, v in safe_data.items()},
                    "endpoint": normalized_endpoint,
                },
                response_data={
                    "status_code": response.status_code,
                    "reason": response.reason,
                    "headers": dict(response.headers),
                    "text": body_text[:4000],
                },
            )
            if response.status_code >= 500:
                snippet = body_text[:400].replace("\n", " ")
                logger.error(
                    "ResellerClub POST %s HTTP %s: %s",
                    normalized_endpoint,
                    response.status_code,
                    snippet,
                )
                raise ResellerClubError(
                    f"ResellerClub HTTP {response.status_code} on {normalized_endpoint}: "
                    f"{snippet or response.reason}. "
                    "This is often a bad/missing contact field (type, company, phone) "
                    "or wrong API environment (live vs test.httpapi.com)."
                )
            response.raise_for_status()
            # Contact add returns a bare integer ID in many LogicBoxes versions.
            try:
                result = response.json()
            except ValueError:
                result = body_text.strip()
            if isinstance(result, (int, float)) or (isinstance(result, str) and result.isdigit()):
                result = {"contact_id": str(result), "id": str(result)}
            if isinstance(result, str):
                # Sometimes a bare quoted number
                stripped = result.strip().strip('"')
                if stripped.isdigit():
                    result = {"contact_id": stripped, "id": stripped}
                else:
                    raise ResellerClubError(f"Unexpected non-JSON response from {normalized_endpoint}: {stripped[:200]}")
        except ResellerClubError:
            raise
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
                    "data": {k: ("***" if k == "api-key" else v) for k, v in safe_data.items()},
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
                raise ResellerClubError(
                    f"ResellerClub HTTP {status_code} on {normalized_endpoint}: {body or e}"
                ) from e
            logger.error(f"ResellerClub POST {normalized_endpoint} failed: {e}")
            raise ResellerClubError(f"API POST failed: {e}") from e
        except requests.RequestException as e:
            self._capture_debug(
                request_data={
                    "method": "POST",
                    "url": url,
                    "headers": {},
                    "body": "",
                    "data": {k: ("***" if k == "api-key" else v) for k, v in safe_data.items()},
                    "endpoint": normalized_endpoint,
                },
                response_data=None,
                error=str(e),
            )
            logger.error(f"ResellerClub POST {normalized_endpoint} failed: {e}")
            # Unwrap RetryError messaging for operators
            msg = str(e)
            if "too many 500" in msg or "RetryError" in type(e).__name__:
                raise ResellerClubError(
                    f"ResellerClub POST {normalized_endpoint} failed after retries: {msg}. "
                    "HTTP 500 usually means invalid contact payload or API/credentials issue — "
                    "not a transient network blip."
                ) from e
            raise ResellerClubError(f"API POST failed: {e}") from e
        return self._check_response(result, normalized_endpoint)

    # ResellerClub permits many TLDs per availability call but URLs grow long;
    # chunk to keep query strings under typical 8KB limits.
    _AVAILABILITY_TLD_CHUNK = 30

    def check_availability(self, domain_names: list, tlds: list) -> dict:
        """
        Check availability of domain names across TLDs.
        Returns dict of "<domain>.<tld>" -> availability info.

        Per the LogicBoxes spec, ``domain-name`` and ``tlds`` MUST be sent as
        REPEATED query parameters (e.g. ``tlds=com&tlds=net``) — comma-joined
        values are treated by the API as a single literal TLD and produce
        ``{"<label>.<comma-joined>": {"status": "unknown"}}``.
        """
        labels = self._normalize_domain_labels(domain_names)
        normalized_tlds = self._normalize_tlds(tlds)
        if not labels or not normalized_tlds:
            raise ResellerClubError("Domain availability check requires at least one domain label and one TLD.")

        merged: dict = {}
        # Chunk TLDs to keep the URL within safe limits.
        for i in range(0, len(normalized_tlds), self._AVAILABILITY_TLD_CHUNK):
            chunk = normalized_tlds[i : i + self._AVAILABILITY_TLD_CHUNK]
            params = {
                # Lists cause requests to repeat the param: domain-name=a&domain-name=b
                "domain-name": labels,
                "tlds": chunk,
            }
            data = self._get("domains/available", params)
            if isinstance(data, dict):
                merged.update(data)
        return merged

    def discover_tld_classkeys(self, tlds: list, probe_label: str = "example") -> dict:
        """
        Return a ``{tld: classkey}`` mapping by issuing availability lookups.

        ResellerClub identifies products by a short ``classkey`` (for example
        ``domcno`` for .com, ``thirdleveldotuk`` for .co.uk).  The classkey is
        included in every availability response and is the join key for the
        customer pricing catalog, so this method gives us the bridge between
        a friendly TLD string and the pricing payload.
        """
        normalized = self._normalize_tlds(tlds)
        if not normalized:
            return {}
        response = self.check_availability([probe_label], normalized)
        prefix = f"{probe_label}."
        result: dict = {}
        for full_domain, info in (response or {}).items():
            if not isinstance(info, dict):
                continue
            classkey = info.get("classkey")
            if not classkey or not full_domain.startswith(prefix):
                continue
            tld = full_domain[len(prefix):].strip().lower()
            if tld:
                result[tld] = str(classkey)
        # Cache for later get_tld_costs() calls.
        self._tld_classkeys.update(result)
        return result

    def get_customer_pricing(self) -> dict:
        """
        Fetch the FULL ResellerClub customer pricing catalog in one request.

        The ``products/customer-price.json`` endpoint takes only auth params
        and returns a dict keyed by ``classkey`` with sub-dicts for each
        action (``addnewdomain``, ``renewdomain``, ``addtransferdomain``,
        ``restoredomain``) mapping number-of-years strings to prices.

        Result is cached on the client instance for the lifetime of the
        instance to avoid the heavy (~120KB) repeat call.
        """
        if self._pricing_catalog is None:
            self._pricing_catalog = self._get("products/customer-price")
        return self._pricing_catalog

    def prime_pricing_cache(self, tlds: list) -> None:
        """Pre-populate classkey + catalog caches for the given TLD list."""
        self.discover_tld_classkeys(tlds)
        self.get_customer_pricing()

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

    # Mapping from logical action -> key used inside the customer pricing catalog.
    _ACTION_TO_CATALOG_KEY = {
        "registration": "addnewdomain",
        "renewal": "renewdomain",
        "transfer": "addtransferdomain",
        "restore": "restoredomain",
    }

    def get_tld_pricing(self, tld: str, years: int = 1, action: str = "registration") -> dict:
        """Return the pricing block for a single TLD/action from the catalog.

        The returned dict matches the catalog shape for that action, e.g.
        ``{"1": 9.50, "2": 9.50, ...}``, plus a ``price`` convenience key
        carrying the price for the requested ``years`` (falling back to 1).
        """
        normalized_tld = self._normalize_tld_value(tld)
        if not normalized_tld:
            return {}
        catalog_action = self._ACTION_TO_CATALOG_KEY.get(action, action)

        classkey = self._tld_classkeys.get(normalized_tld)
        if not classkey:
            self.discover_tld_classkeys([normalized_tld])
            classkey = self._tld_classkeys.get(normalized_tld)
        if not classkey:
            return {}

        catalog = self.get_customer_pricing() or {}
        tld_block = catalog.get(classkey) or {}
        action_block = tld_block.get(catalog_action) or {}
        if not isinstance(action_block, dict):
            return {}

        # Pick the requested years, fall back to "1".
        price = action_block.get(str(years))
        if price is None:
            price = action_block.get("1")
        result = dict(action_block)
        if price is not None:
            result["price"] = price
        result["classkey"] = classkey
        result["tld"] = normalized_tld
        result["action"] = catalog_action
        return result

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

    # ResellerClub/LogicBoxes has no API endpoint for discovering available TLDs.
    # This curated list covers the TLDs they support for registration and pricing.
    # Extend this list as needed when ResellerClub adds new TLDs to your reseller account.
    SUPPORTED_TLDS = [
        # Popular generic TLDs
        "com", "net", "org", "info", "biz", "name", "mobi", "tel", "asia",
        # UK
        "co.uk", "org.uk", "me.uk", "uk",
        # European ccTLDs
        "de", "fr", "es", "it", "nl", "be", "eu", "at", "ch", "dk", "se",
        "no", "fi", "pl", "cz", "hu", "ro", "pt", "gr", "tr", "ru", "ua",
        # Americas
        "us", "ca", "com.mx", "mx", "com.ar", "com.br",
        # Asia-Pacific
        "com.au", "net.au", "org.au", "co.nz", "nz", "in", "co.in", "net.in",
        "org.in", "hk", "tw", "sg", "cn", "jp", "co.kr",
        # New gTLDs — commonly offered by ResellerClub resellers
        "co", "io", "me", "tv", "cc",
        "club", "online", "site", "website", "tech", "store", "shop",
        "blog", "digital", "media", "email", "space", "host", "press",
        "design", "studio", "agency", "solutions", "services", "support",
        "expert", "works", "systems", "group", "network", "team",
        "today", "center", "business", "management", "properties",
        "estate", "land", "house", "foundation", "education", "school",
        "training", "institute", "academy", "science", "energy",
        "solar", "green", "photography", "video", "film", "art", "gallery",
        "band", "music", "chat", "social", "community", "life",
        "health", "care", "clinic", "dental", "doctor", "lawyer", "legal",
        "finance", "financial", "consulting", "marketing", "events",
        "wedding", "holiday", "travel", "hotel", "tours", "guide",
        "news", "link", "click", "ninja", "guru", "rocks", "tips",
        "tools", "codes", "pro",
    ]

    def list_available_tlds(self) -> list:
        """
        Return the curated list of TLDs supported by ResellerClub.

        The ResellerClub/LogicBoxes HTTP API does not provide any endpoint for
        dynamically discovering available TLDs — attempts to call such endpoints
        will 404.  This returns a built-in curated list instead; add TLDs here
        as ResellerClub makes new ones available on your reseller account.
        """
        return list(self.SUPPORTED_TLDS)

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

    def transfer_domain(
        self,
        domain_name: str,
        customer_id: str,
        reg_contact_id: str,
        admin_contact_id: str,
        tech_contact_id: str,
        billing_contact_id: str,
        nameservers: list,
        auth_code: str = "",
        auto_renew: bool = True,
    ) -> dict:
        """Transfer a domain name into the registrar account."""
        data = {
            "domain-name": domain_name,
            "customer-id": customer_id,
            "reg-contact-id": reg_contact_id,
            "admin-contact-id": admin_contact_id,
            "tech-contact-id": tech_contact_id,
            "billing-contact-id": billing_contact_id,
            "ns": nameservers,
            "auto-renew": auto_renew,
        }
        if auth_code:
            data["auth-code"] = auth_code
        return self._post("domains/transfer", data)

    def get_order_details(self, order_id: str) -> dict:
        """Get details for a domain order."""
        return self._get("domains/details", {"order-id": order_id, "options": "All"})

    @staticmethod
    def _epoch_to_iso(value):
        try:
            epoch = int(value)
        except (TypeError, ValueError):
            return ""
        if epoch <= 0:
            return ""
        return datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()

    @staticmethod
    def _first_text_from_order(record: dict, *keys: str) -> str:
        if not isinstance(record, dict):
            return ""
        for key in keys:
            value = record.get(key)
            if value not in (None, ""):
                return str(value).strip()
        details = record.get("order_details")
        if isinstance(details, dict):
            for key in keys:
                value = details.get(key)
                if value not in (None, ""):
                    return str(value).strip()
        return ""

    @classmethod
    def _extract_domain_name_from_order(cls, record: dict) -> str:
        return cls._first_text_from_order(
            record,
            "domainname",
            "domain-name",
            "domain_name",
            "domain",
            "description",
        ).lower().rstrip(".")

    @classmethod
    def _extract_order_id_from_order(cls, record: dict) -> str:
        return cls._first_text_from_order(
            record,
            "orderid",
            "order-id",
            "order_id",
            "entityid",
            "entity-id",
            "entity_id",
        )

    @classmethod
    def _extract_status_from_order(cls, record: dict) -> str:
        return cls._first_text_from_order(
            record,
            "currentstatus",
            "current-status",
            "current_status",
            "status",
            "orderstatus",
            "order-status",
            "order_status",
        )

    def list_domain_orders(
        self,
        page_no: int = 1,
        no_of_records: int = 100,
        status: str = "Active",
        include_details: bool = False,
        max_details: int = 100,
    ) -> list[dict]:
        """Return registrar domain orders with normalized dates and optional full details."""
        payload = self._get(
            "domains/search",
            {
                "page-no": page_no,
                "no-of-records": no_of_records,
                "status": status,
            },
        )

        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            extracted = payload.get("orders") or payload.get("data") or payload.get("results")
            if isinstance(extracted, list):
                records = extracted
            elif isinstance(extracted, dict):
                # Some LogicBoxes responses return a map keyed by order id.
                records = [v for v in extracted.values() if isinstance(v, dict)]
            else:
                # Fallback for payloads that are themselves an order-id keyed map.
                records = []
                for key, value in payload.items():
                    if not isinstance(value, dict):
                        continue
                    if not any(
                        marker in value
                        for marker in (
                            "domainname",
                            "domain-name",
                            "domain_name",
                            "domain",
                            "description",
                            "orderid",
                            "order-id",
                            "order_id",
                            "entityid",
                            "currentstatus",
                            "current-status",
                            "current_status",
                            "status",
                            "endtime",
                            "creationtime",
                        )
                    ):
                        continue
                    row = dict(value)
                    if not row.get("orderid") and str(key).isdigit():
                        row["orderid"] = str(key)
                    records.append(row)
        else:
            records = []

        normalized = []
        for idx, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            item = dict(record)
            domain_name = self._extract_domain_name_from_order(item)
            order_id = self._extract_order_id_from_order(item)
            current_status = self._extract_status_from_order(item)
            if domain_name:
                item["domainname"] = domain_name
            if order_id:
                item["orderid"] = order_id
            if current_status:
                item["currentstatus"] = current_status
            item["expiry_date"] = self._epoch_to_iso(record.get("endtime"))
            item["creation_date"] = self._epoch_to_iso(record.get("creationtime"))

            if include_details and idx < max_details:
                order_id = str(item.get("orderid") or "").strip()
                if order_id:
                    try:
                        details = self.get_order_details(order_id)
                        item["order_details"] = details
                        # Backfill common fields from details if list payload omits them.
                        if isinstance(details, dict):
                            detail_domain = self._extract_domain_name_from_order(details)
                            detail_status = self._extract_status_from_order(details)
                            if detail_domain:
                                item["domainname"] = item.get("domainname") or detail_domain
                            if detail_status:
                                item["currentstatus"] = item.get("currentstatus") or detail_status
                            item.setdefault("recurring", details.get("recurring"))
                            item.setdefault("endtime", details.get("endtime"))
                            item.setdefault("creationtime", details.get("creationtime"))
                            item["expiry_date"] = item["expiry_date"] or self._epoch_to_iso(details.get("endtime"))
                            item["creation_date"] = item["creation_date"] or self._epoch_to_iso(details.get("creationtime"))
                    except Exception as exc:
                        item["order_details_error"] = str(exc)

            normalized.append(item)
        return normalized

    def list_all_domain_orders(
        self,
        no_of_records: int = 100,
        status: str = "All",
        include_details: bool = False,
        max_details: int = 100,
        max_pages: int = 50,
    ) -> list[dict]:
        """Return all registrar domain orders across pages with best-effort dedupe."""
        all_rows: list[dict] = []
        seen_keys: set[str] = set()
        details_budget = max_details

        for page_no in range(1, max_pages + 1):
            rows = self.list_domain_orders(
                page_no=page_no,
                no_of_records=no_of_records,
                status=status,
                include_details=include_details and details_budget > 0,
                max_details=max(0, details_budget),
            )
            if not rows:
                break

            if include_details:
                details_budget = max(0, details_budget - len(rows))

            for row in rows:
                key = str(row.get("orderid") or row.get("domainname") or "").strip().lower()
                if key and key in seen_keys:
                    continue
                if key:
                    seen_keys.add(key)
                all_rows.append(row)

            if len(rows) < no_of_records:
                break

        return all_rows

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

    def list_customers(self, page_no: int = 1, no_of_records: int = 50) -> list[dict]:
        """Search ResellerClub customer accounts."""
        payload = self._get(
            "customers/search",
            {"page-no": page_no, "no-of-records": no_of_records},
        )
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            extracted = payload.get("customers") or payload.get("data") or payload.get("results")
            if isinstance(extracted, list):
                return [row for row in extracted if isinstance(row, dict)]
            rows = []
            for key, value in payload.items():
                if isinstance(value, dict) and any(
                    marker in value for marker in ("username", "emailaddr", "email", "customerid", "name")
                ):
                    row = dict(value)
                    row.setdefault("customerid", str(key))
                    rows.append(row)
            return rows
        return []

    def list_dns_records(self, order_id: str, record_types: list[str] | None = None) -> list[dict]:
        """Return DNS records from ResellerClub DNS for an order."""
        types = record_types or ["A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA"]
        records: list[dict] = []
        for record_type in types:
            try:
                payload = self._get(
                    "dns/manage/search-records",
                    {
                        "order-id": order_id,
                        "type": record_type,
                        "no-of-records": 50,
                        "page-no": 1,
                    },
                )
            except Exception as exc:
                logger.warning("ResellerClub DNS search failed type=%s order=%s: %s", record_type, order_id, exc)
                continue
            rows = []
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                extracted = payload.get("records") or payload.get("data") or payload.get("recds")
                if isinstance(extracted, list):
                    rows = extracted
                else:
                    rows = [v for v in payload.values() if isinstance(v, dict)]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item = dict(row)
                item.setdefault("type", record_type)
                records.append(item)
        return records

    def update_dns_record(
        self,
        order_id: str,
        host: str,
        current_value: str,
        new_value: str,
        record_type: str,
        ttl: int = 3600,
    ) -> dict:
        data = {
            "order-id": order_id,
            "host": host,
            "current-value": current_value,
            "new-value": new_value,
            "type": record_type,
            "ttl": ttl,
        }
        return self._post("dns/manage/update-record", data)

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
