import logging
import re
import requests

from apps.core.runtime_settings import get_runtime_setting

logger = logging.getLogger(__name__)


class CloudflareError(Exception):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class CloudflareService:
    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self):
        self.token = get_runtime_setting("CLOUDFLARE_API_TOKEN", "")
        self.account_id = get_runtime_setting("CLOUDFLARE_ACCOUNT_ID", "")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _request(self, method, endpoint, **kwargs):
        if not self.token:
            raise CloudflareError("Cloudflare is not configured.")
        url = f"{self.BASE_URL}/{endpoint}"
        mutation = method.upper() not in {"GET", "HEAD", "OPTIONS"}
        try:
            response = requests.request(method, url, headers=self.headers,
                timeout=(10, 30), allow_redirects=False, **kwargs)
            if not 200 <= response.status_code < 300:
                raise CloudflareError("Cloudflare did not accept the request.",
                    uncertain=mutation and (response.status_code >= 500 or 300 <= response.status_code < 400))
            data = response.json()
        except (requests.RequestException, ValueError):
            raise CloudflareError("The Cloudflare request outcome requires review.", uncertain=mutation) from None
        if type(data) is not dict or data.get("success") is not True:
            raise CloudflareError("Cloudflare returned an unsuccessful or unrecognized response.", uncertain=mutation)
        return data

    @staticmethod
    def _account_id(value):
        if type(value) is not str or re.fullmatch(r"[a-fA-F0-9]{32}", value) is None:
            raise CloudflareError("Choose a valid Cloudflare account identifier.")
        return value

    @staticmethod
    def _zone_name(value):
        from django.core.validators import validate_domain_name
        from django.core.exceptions import ValidationError
        if type(value) is not str:
            raise CloudflareError("Choose a valid Cloudflare zone name.")
        value = value.strip().lower().rstrip(".")
        try:
            validate_domain_name(value)
        except ValidationError:
            raise CloudflareError("Choose a valid Cloudflare zone name.") from None
        return value

    def list_zones(self, zone_name, *, account_id, page=1):
        if type(page) is not int or not 1 <= page <= 10000:
            raise CloudflareError("Choose a valid Cloudflare result page.")
        return self._request("GET", "zones", params={"name": self._zone_name(zone_name),
            "account.id": self._account_id(account_id), "page": page, "per_page": 50, "match": "all"})

    def create_zone(self, zone_name, jump_start=False, *, account_id=None):
        if type(jump_start) is not bool:
            raise CloudflareError("Choose an explicit Cloudflare discovery preference.")
        data = {"name": self._zone_name(zone_name), "jump_start": jump_start}
        if account_id is not None:
            data["account"] = {"id": self._account_id(account_id)}
        return self._request("POST", "zones", json=data)

    def get_zone(self, zone_id):
        return self._request("GET", f"zones/{zone_id}")

    def list_dns_records(self, zone_id, *, page=None, per_page=200):
        if page is None:
            return self._request("GET", f"zones/{zone_id}/dns_records")
        if type(page) is not int or not 1 <= page <= 10000 or type(per_page) is not int or not 1 <= per_page <= 200:
            raise CloudflareError("Choose valid Cloudflare DNS pagination.")
        if type(zone_id) is not str or re.fullmatch(r"[a-fA-F0-9]{32}", zone_id) is None:
            raise CloudflareError("Choose a valid Cloudflare zone identifier.")
        return self._request("GET", f"zones/{zone_id}/dns_records", params={"page": page, "per_page": per_page, "order": "name"})

    def create_dns_record(self, zone_id, record_type, name, content, ttl=3600, proxied=False):
        data = {"type": record_type, "name": name, "content": content, "ttl": ttl, "proxied": proxied}
        return self._request("POST", f"zones/{zone_id}/dns_records", json=data)

    def update_dns_record(self, zone_id, record_id, **kwargs):
        return self._request("PUT", f"zones/{zone_id}/dns_records/{record_id}", json=kwargs)

    def delete_dns_record(self, zone_id, record_id):
        return self._request("DELETE", f"zones/{zone_id}/dns_records/{record_id}")
