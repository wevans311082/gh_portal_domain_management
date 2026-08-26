import logging
import requests

from apps.core.runtime_settings import get_runtime_setting

logger = logging.getLogger(__name__)


class CompaniesHouseService:
    BASE_URL = "https://api.company-information.service.gov.uk"
    USER_AGENT = "CyberAsk-Portal/1.0 (https://www.cyberask.co.uk; domains@cyberask.co.uk)"

    def __init__(self):
        self.api_key = str(get_runtime_setting("COMPANIES_HOUSE_API_KEY", "") or "").strip()
        if len(self.api_key) >= 2 and self.api_key[0] == self.api_key[-1] and self.api_key[0] in {"'", '"'}:
            self.api_key = self.api_key[1:-1].strip()
        self.last_error = ""
        self.last_status_code = None

    def _request(self, url, *, params=None):
        self.last_error = ""
        self.last_status_code = None
        if not self.api_key:
            self.last_error = "Companies House API key is not configured."
            return None
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json",
        }
        try:
            response = requests.get(
                url,
                params=params,
                auth=(self.api_key, ""),
                headers=headers,
                timeout=12,
            )
        except requests.RequestException as exc:
            self.last_error = f"Request failed: {exc}"
            logger.warning("Companies House request failed: %s", exc)
            return None
        self.last_status_code = response.status_code
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                self.last_error = "Companies House returned a non-JSON response."
                return None
        snippet = (response.text or "").strip().replace("\n", " ")[:280]
        if response.status_code == 401:
            self.last_error = "Authentication failed. Check the API key."
        elif response.status_code == 403:
            self.last_error = (
                "Companies House rejected the request (HTTP 403). "
                "Register a REST API key at developer.company-information.service.gov.uk "
                "and include a User-Agent. " + snippet
            )
        elif response.status_code == 404:
            self.last_error = "Company not found."
        elif response.status_code == 429:
            self.last_error = "Companies House rate limit exceeded. Try again shortly."
        else:
            self.last_error = f"HTTP {response.status_code}: {snippet or 'empty response'}"
        logger.warning("Companies House %s -> %s", url, self.last_error)
        return None

    def get_company(self, company_number):
        normalized = (company_number or "").strip().replace(" ", "").upper()
        if not normalized:
            self.last_error = "Enter a company number."
            return None
        url = f"{self.BASE_URL}/company/{normalized}"
        return self._request(url)

    def search_companies(self, query, items_per_page=10):
        url = f"{self.BASE_URL}/search/companies"
        params = {"q": (query or "").strip(), "items_per_page": items_per_page}
        if not params["q"]:
            self.last_error = "Enter a search query."
            return None
        return self._request(url, params=params)
