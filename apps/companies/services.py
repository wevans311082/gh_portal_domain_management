import logging
import requests

from apps.core.runtime_settings import get_runtime_setting

logger = logging.getLogger(__name__)


class CompaniesHouseService:
    BASE_URL = "https://api.company-information.service.gov.uk"

    def __init__(self):
        self.api_key = get_runtime_setting("COMPANIES_HOUSE_API_KEY", "")

    def _request(self, url, *, params=None):
        if not self.api_key:
            return None
        try:
            response = requests.get(url, params=params, auth=(self.api_key, ""), timeout=12)
        except requests.RequestException as exc:
            logger.warning("Companies House request failed: %s", exc)
            return None
        if response.status_code == 200:
            return response.json()
        return None

    def get_company(self, company_number):
        normalized = (company_number or "").strip().replace(" ", "").upper()
        if not normalized:
            return None
        url = f"{self.BASE_URL}/company/{normalized}"
        return self._request(url)

    def search_companies(self, query, items_per_page=10):
        url = f"{self.BASE_URL}/search/companies"
        params = {"q": (query or "").strip(), "items_per_page": items_per_page}
        if not params["q"]:
            return None
        return self._request(url, params=params)
