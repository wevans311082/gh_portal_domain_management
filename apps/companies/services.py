import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class CompaniesHouseService:
    BASE_URL = "https://api.company-information.service.gov.uk"

    def __init__(self):
        self.api_key = settings.COMPANIES_HOUSE_API_KEY

    def get_company(self, company_number):
        url = f"{self.BASE_URL}/company/{company_number}"
        response = requests.get(url, auth=(self.api_key, ""))
        if response.status_code == 200:
            return response.json()
        return None

    def search_companies(self, query, items_per_page=10):
        url = f"{self.BASE_URL}/search/companies"
        params = {"q": query, "items_per_page": items_per_page}
        response = requests.get(url, params=params, auth=(self.api_key, ""))
        if response.status_code == 200:
            return response.json()
        return None
