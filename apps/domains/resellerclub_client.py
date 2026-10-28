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
        """Provider error messages are untrusted and can contain secrets."""
        if isinstance(data, dict) and str(data.get("status", "")).upper() == "ERROR":
            logger.warning("ResellerClub %s rejected the request", endpoint)
            raise ResellerClubError("The registrar rejected the request. Review its retained operation and provider case before continuing.")
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
        # Provider bodies can echo arbitrary credentials, EPP codes and PII.
        # Keep only transport metadata; key-name redaction cannot sanitize them.
        add_entry({"request": {"method": request_data.get("method"), "endpoint": request_data.get("endpoint")},
                   "response": {"status_code": response_data.get("status_code")} if response_data else None,
                   "error": "Transport failed" if error else ""})

    def _request(self, method, endpoint, values):
        self.ensure_configured()
        normalized = self._normalize_endpoint(endpoint)
        merged = {**self._auth_params, **(values or {})}
        options = {"timeout": (_CONNECT_TIMEOUT, _READ_TIMEOUT), "allow_redirects": False}
        if method == "POST":
            options["data"] = {key: "true" if value is True else "false" if value is False else value
                               for key, value in merged.items() if value is not None}
        else:
            options["params"] = merged
        metadata = {"method": method, "endpoint": normalized}
        try:
            response = getattr(self.session, method.lower())(f"{self.base_url}/{normalized}", **options)
            self._capture_debug(metadata, {"status_code": response.status_code})
            if 300 <= response.status_code < 400:
                raise ResellerClubError("The registrar redirected the request. Review its configured endpoint before continuing.")
            response.raise_for_status()
            try:
                result = response.json()
            except ValueError:
                if method != "POST":
                    raise ResellerClubError("The registrar returned an invalid response. Review the retained operation before retrying.") from None
                result = (response.text or "").strip()
            if method == "POST":
                if type(result) in (int, float) or isinstance(result, str) and result.strip().strip('"').isdigit():
                    identifier = str(result).strip().strip('"')
                    result = {"contact_id": identifier, "id": identifier}
                elif isinstance(result, str):
                    raise ResellerClubError("The registrar returned an unexpected response. Review the retained operation before retrying.")
            return self._check_response(result, normalized)
        except requests.RequestException:
            self._capture_debug(metadata, error="transport")
            logger.warning("ResellerClub %s %s transport failed", method, normalized)
            raise ResellerClubError("The registrar request failed. Review the retained operation before retrying.") from None

    def _get(self, endpoint: str, params: dict = None) -> dict:
        return self._request("GET", endpoint, params)

    def _post(self, endpoint: str, data: dict = None) -> dict:
        return self._request("POST", endpoint, data)

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
        registrant_org_consent: bool = False,
    ) -> dict:
        """Register a domain name."""
        from apps.domains.registrar_policy import policy_for, registration_contact_ids, tld_for_domain
        policy = policy_for(tld_for_domain(domain_name))
        from apps.domains.registry_requirements import validate_registration, validate_nameservers
        validate_registration(domain_name, years=years)
        validate_nameservers(domain_name, nameservers)
        reg_contact_id, admin_contact_id, tech_contact_id, billing_contact_id = registration_contact_ids(
            domain_name, reg_contact_id, admin_contact_id, tech_contact_id, billing_contact_id
        )
        data = {
            "domain-name": domain_name,
            "years": years,
            "ns": nameservers,
            "customer-id": customer_id,
            "reg-contact-id": reg_contact_id,
            "admin-contact-id": admin_contact_id,
            "tech-contact-id": tech_contact_id,
            "billing-contact-id": billing_contact_id,
            "invoice-option": "NoInvoice",
            "purchase-privacy": bool(purchase_privacy and policy.privacy_supported),
            "auto-renew": auto_renew,
        }
        from apps.domains.publication_consent import publication_attributes
        data.update(publication_attributes(domain_name, registrant_org_consent))
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
        registrant_org_consent: bool = False,
    ) -> dict:
        """Transfer a domain name into the registrar account."""
        from django.core.exceptions import ValidationError
        from apps.domains.registrar_policy import ALL_ROLES, policy_for, tld_for_domain
        if policy_for(tld_for_domain(domain_name)).roles != ALL_ROLES:
            raise ValidationError("This extension requires a registrar-specific transfer workflow. Contact support before submitting a transfer.")
        data = {
            "domain-name": domain_name,
            "customer-id": customer_id,
            "reg-contact-id": reg_contact_id,
            "admin-contact-id": admin_contact_id,
            "tech-contact-id": tech_contact_id,
            "billing-contact-id": billing_contact_id,
            "ns": nameservers,
            "auto-renew": auto_renew,
            "invoice-option": "NoInvoice",
        }
        if auth_code:
            data["auth-code"] = auth_code
        from apps.domains.publication_consent import publication_attributes
        data.update(publication_attributes(domain_name, registrant_org_consent))
        return self._post("domains/transfer", data)

    def get_order_details(self, order_id: str) -> dict:
        """Get details for a domain order."""
        payload = self._get("domains/details", {"order-id": order_id, "options": "All"})
        return self._flatten_logicboxes_record(payload) if isinstance(payload, dict) else payload

    def get_registrant_verification_details(self, order_id: str) -> dict:
        """Fetch the identity-binding and RAA fields required for reconciliation."""
        payload = self._get("domains/details", {
            "order-id": order_id,
            "options": ["OrderDetails", "ContactIds", "RegistrantContactDetails", "DomainStatus"],
        })
        return self._flatten_logicboxes_record(payload) if isinstance(payload, dict) else payload

    def get_registration_reconciliation_details(self, order_id: str) -> dict:
        """Read existing-order identity, delegation, dates and current status."""
        payload = self._get("domains/details", {
            "order-id": order_id,
            "options": ["OrderDetails", "ContactIds", "NsDetails", "StatusDetails"],
        })
        return self._flatten_logicboxes_record(payload) if isinstance(payload, dict) else payload

    def get_publication_reconciliation_details(self, order_id: str) -> dict:
        """Read scoped contact IDs and the documented registrantOrgConsentDetails."""
        from apps.domains.contact_change_confirmation import identifier
        if not identifier(order_id):
            raise ValueError("A valid registrar order is required.")
        payload = self._get("domains/details", {"order-id": identifier(order_id),
            "options": ["OrderDetails", "ContactIds", "RegistrantContactDetails", "StatusDetails"]})
        return self._flatten_logicboxes_record(payload) if isinstance(payload, dict) else payload

    def resend_registrant_verification(self, order_id: str) -> bool:
        """Request one RAA email; only literal JSON true confirms acceptance."""
        result = self._post("domains/raa/resend-verification", {"order-id": order_id})
        if type(result) is not bool:
            raise ResellerClubError("The registrar returned an uncertain verification-email result.")
        return result

    def get_order_details_by_name(self, domain_name: str) -> dict:
        """Get details for a domain order by its full domain name."""
        payload = self._get(
            "domains/details-by-name",
            {"domain-name": domain_name, "options": "All"},
        )
        return self._flatten_logicboxes_record(payload) if isinstance(payload, dict) else payload

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
    def _flatten_logicboxes_record(record: dict) -> dict:
        """Copy dotted LogicBoxes keys (``orders.orderid``) to short aliases (``orderid``)."""
        if not isinstance(record, dict):
            return {}
        out = dict(record)
        for key, value in record.items():
            text = str(key)
            if "." not in text:
                continue
            short = text.split(".")[-1]
            if short and (short not in out or out[short] in (None, "")):
                out[short] = value
        details = record.get("order_details")
        if isinstance(details, dict):
            for key, value in details.items():
                text = str(key)
                short = text.split(".")[-1] if "." in text else text
                if short and (short not in out or out[short] in (None, "")):
                    out[short] = value
        return out

    @classmethod
    def _first_text_from_order(cls, record: dict, *keys: str) -> str:
        if not isinstance(record, dict):
            return ""
        wanted = {str(key).lower() for key in keys}
        sources = [record]
        details = record.get("order_details")
        if isinstance(details, dict):
            sources.append(details)
        for source in sources:
            for key in keys:
                value = source.get(key)
                if value not in (None, ""):
                    return str(value).strip()
            for rec_key, rec_val in source.items():
                short = str(rec_key).split(".")[-1].lower()
                if short in wanted and rec_val not in (None, ""):
                    return str(rec_val).strip()
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

    _SEARCH_META_KEYS = {
        "recsonpage",
        "recsindb",
        "status",
        "message",
        "error",
        "errorcode",
    }
    _SEARCH_RECORD_MARKERS = (
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
        "orders.orderid",
        "entity.description",
        "entity.currentstatus",
        "entity.entityid",
        "orders.endtime",
        "orders.creationtime",
    )
    _SEARCH_STATUSES = (
        "Active",
        "InActive",
        "Inactive",
        "Suspended",
        "Pending Delete Restorable",
        "PendingDeleteRestorable",
        "Deleted",
        "Archived",
    )

    def _search_params(self, page_no: int, no_of_records: int, status: str | None) -> dict:
        params = {"page-no": page_no, "no-of-records": no_of_records}
        if status and str(status).strip().lower() not in {"all", "*"}:
            params["status"] = status
        return params

    def _records_from_search_payload(self, payload) -> list[dict]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        extracted = payload.get("orders") or payload.get("data") or payload.get("results")
        if isinstance(extracted, list):
            return [row for row in extracted if isinstance(row, dict)]
        if isinstance(extracted, dict):
            return [v for v in extracted.values() if isinstance(v, dict)]

        records = []
        for key, value in payload.items():
            if str(key).lower() in self._SEARCH_META_KEYS:
                continue
            if not isinstance(value, dict):
                continue
            if not any(marker in value for marker in self._SEARCH_RECORD_MARKERS):
                # Still accept numbered LogicBoxes rows after flattening.
                flattened = self._flatten_logicboxes_record(value)
                if not any(marker in flattened for marker in self._SEARCH_RECORD_MARKERS):
                    continue
                value = flattened
            row = self._flatten_logicboxes_record(value)
            if not row.get("orderid") and str(key).isdigit():
                row["orderid"] = str(key)
            records.append(row)
        return records

    def _normalize_order_record(self, record: dict) -> dict:
        item = self._flatten_logicboxes_record(record)
        domain_name = self._extract_domain_name_from_order(item)
        order_id = self._extract_order_id_from_order(item)
        current_status = self._extract_status_from_order(item)
        if domain_name:
            item["domainname"] = domain_name
        if order_id:
            item["orderid"] = order_id
        if current_status:
            item["currentstatus"] = current_status
        item["expiry_date"] = self._epoch_to_iso(item.get("endtime"))
        item["creation_date"] = self._epoch_to_iso(item.get("creationtime"))
        item["customerid"] = self._first_text_from_order(
            item, "customerid", "customer-id", "customer_id"
        )
        ns_list = []
        raw_ns = item.get("ns") or item.get("nserver") or item.get("nameservers")
        if isinstance(raw_ns, list):
            for entry in raw_ns:
                host = entry.get("ns") if isinstance(entry, dict) else str(entry or "")
                if host:
                    ns_list.append(host.strip().lower())
        for idx in range(1, 5):
            host = self._first_text_from_order(item, f"ns{idx}", f"nameserver{idx}")
            if host:
                ns_list.append(host.lower())
            item[f"ns{idx}"] = host.lower() if host else item.get(f"ns{idx}", "")
        seen = []
        for host in ns_list:
            if host and host not in seen:
                seen.append(host)
        item["nameservers"] = seen
        return item

    def list_domain_orders(
        self,
        page_no: int = 1,
        no_of_records: int = 50,
        status: str = "Active",
        include_details: bool = False,
        max_details: int = 100,
    ) -> list[dict]:
        """Return registrar domain orders with normalized dates and optional full details."""
        payload = self._get("domains/search", self._search_params(page_no, no_of_records, status))
        records = self._records_from_search_payload(payload)

        normalized = []
        for idx, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            item = self._normalize_order_record(record)

            if include_details and idx < max_details:
                order_id = str(item.get("orderid") or "").strip()
                domain_name = str(item.get("domainname") or "").strip()
                details = None
                try:
                    if order_id:
                        details = self.get_order_details(order_id)
                    elif domain_name:
                        details = self.get_order_details_by_name(domain_name)
                except Exception as exc:
                    item["order_details_error"] = str(exc)
                if isinstance(details, dict):
                    item["order_details"] = details
                    merged = self._normalize_order_record({**item, **details})
                    merged["order_details"] = details
                    item = merged
            normalized.append(item)
        return normalized

    def _paginate_domain_orders(
        self,
        *,
        status: str | None,
        no_of_records: int,
        include_details: bool,
        max_details: int,
        max_pages: int,
    ) -> list[dict]:
        all_rows: list[dict] = []
        seen_keys: set[str] = set()
        details_budget = max_details
        page_size = min(max(int(no_of_records or 50), 1), 50)

        for page_no in range(1, max_pages + 1):
            rows = self.list_domain_orders(
                page_no=page_no,
                no_of_records=page_size,
                status=status or "",
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
            if len(rows) < page_size:
                break
        return all_rows

    def list_all_domain_orders(
        self,
        no_of_records: int = 50,
        status: str = "All",
        include_details: bool = False,
        max_details: int = 100,
        max_pages: int = 50,
    ) -> list[dict]:
        """Return all registrar domain orders across pages with best-effort dedupe.

        LogicBoxes does not accept ``status=All``. We first search with no status
        filter, then union the documented status values if that returns nothing.
        """
        wanted = (status or "").strip()
        if wanted.lower() in {"", "all", "*"}:
            try:
                rows = self._paginate_domain_orders(
                    status=None,
                    no_of_records=no_of_records,
                    include_details=include_details,
                    max_details=max_details,
                    max_pages=max_pages,
                )
                if rows:
                    return rows
            except ResellerClubError:
                rows = []
            combined: list[dict] = []
            seen: set[str] = set()
            remaining_details = max_details
            for st in self._SEARCH_STATUSES:
                try:
                    chunk = self._paginate_domain_orders(
                        status=st,
                        no_of_records=no_of_records,
                        include_details=include_details,
                        max_details=remaining_details,
                        max_pages=max_pages,
                    )
                except ResellerClubError:
                    continue
                if include_details:
                    remaining_details = max(0, remaining_details - len(chunk))
                for row in chunk:
                    key = str(row.get("orderid") or row.get("domainname") or "").strip().lower()
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    combined.append(row)
            return combined

        return self._paginate_domain_orders(
            status=wanted,
            no_of_records=no_of_records,
            include_details=include_details,
            max_details=max_details,
            max_pages=max_pages,
        )

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
            return [self._flatten_logicboxes_record(row) for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            extracted = payload.get("customers") or payload.get("data") or payload.get("results")
            if isinstance(extracted, list):
                return [self._flatten_logicboxes_record(row) for row in extracted if isinstance(row, dict)]
            rows = []
            for key, value in payload.items():
                if str(key).lower() in self._SEARCH_META_KEYS or not isinstance(value, dict):
                    continue
                row = self._flatten_logicboxes_record(value)
                if not any(
                    marker in row or marker in value
                    for marker in ("username", "emailaddr", "email", "customerid", "name", "company")
                ):
                    continue
                if not row.get("customerid") and str(key).isdigit():
                    row["customerid"] = str(key)
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
        """The registrar deprecated contacts/modify; never mutate shared contacts."""
        raise ValueError("Registrar contacts are immutable. Create and associate a new contact binding.")

    def get_contact(self, contact_id: str) -> dict:
        """Fetch a single domain contact from ResellerClub."""
        return self._get("contacts/details", {"contact-id": contact_id})

    def change_domain_contacts(self, order_id, role_ids, *, domain_name, registrant_org_consent=False):
        """Associate immutable contacts without bypassing registrant authorization."""
        from apps.domains.contact_change_confirmation import identifier
        from apps.domains.publication_consent import publication_attributes
        from apps.domains.registrar_policy import registration_contact_ids
        if not identifier(order_id) or not isinstance(role_ids, dict):
            raise ValueError("A valid registrar order and contact map are required.")
        values = registration_contact_ids(domain_name, *(role_ids.get(role) for role in ("reg", "admin", "tech", "billing")))
        if any(value != "-1" and not identifier(value) for value in values):
            raise ValueError("Valid registrar contacts are required for every applicable role.")
        data = {
            "order-id": identifier(order_id), "reg-contact-id": values[0], "admin-contact-id": values[1],
            "tech-contact-id": values[2], "billing-contact-id": values[3],
            "designated-agent": False, "sixty-day-lock-optout": False,
        }
        data.update(publication_attributes(domain_name, registrant_org_consent))
        return self._post("domains/modify-contact", data)

    def get_irtp_status(self, order_id):
        # The current 2804 contract documents POST for this read-only lookup.
        from apps.domains.contact_change_confirmation import identifier
        if not identifier(order_id):
            raise ValueError("A valid registrar order is required.")
        return self._post("domains/irtp/verification/details", {"order-id": identifier(order_id)})

    def get_contact_change_action(self, order_id, action_id):
        """Bounded read of one exact action; absence in current is not completion."""
        from apps.domains.contact_change_confirmation import identifier, action_observation
        if not identifier(order_id) or not identifier(action_id):
            raise ValueError("A valid registrar order and action are required.")
        parameters = {"order-id": [identifier(order_id)], "eaq-id": [identifier(action_id)],
                      "no-of-records": 2, "page-no": 1}
        for endpoint in ("actions/search-current", "actions/search-archived"):
            result = action_observation(self._get(endpoint, parameters), order_id=order_id, action_id=action_id)
            if result is not None:
                return result
        return None
