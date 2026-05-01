import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class CloudflareService:
    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self):
        self.token = settings.CLOUDFLARE_API_TOKEN
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _request(self, method, endpoint, **kwargs):
        url = f"{self.BASE_URL}/{endpoint}"
        response = requests.request(method, url, headers=self.headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def get_zone(self, zone_id):
        return self._request("GET", f"zones/{zone_id}")

    def list_dns_records(self, zone_id):
        return self._request("GET", f"zones/{zone_id}/dns_records")

    def create_dns_record(self, zone_id, record_type, name, content, ttl=3600, proxied=False):
        data = {"type": record_type, "name": name, "content": content, "ttl": ttl, "proxied": proxied}
        return self._request("POST", f"zones/{zone_id}/dns_records", json=data)

    def update_dns_record(self, zone_id, record_id, **kwargs):
        return self._request("PUT", f"zones/{zone_id}/dns_records/{record_id}", json=kwargs)

    def delete_dns_record(self, zone_id, record_id):
        return self._request("DELETE", f"zones/{zone_id}/dns_records/{record_id}")
