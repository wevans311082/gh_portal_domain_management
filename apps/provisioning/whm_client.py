"""WHM/cPanel API client for hosting account provisioning."""
import logging
import secrets
import string
import requests

from apps.core.runtime_settings import get_runtime_int, get_runtime_setting

logger = logging.getLogger(__name__)


class WHMClientError(Exception):
    """Raised when WHM API returns an error."""
    pass


class WHMClient:
    """Client for the WHM JSON API v1."""

    # Cache endpoint support across client instances in this process to avoid
    # repeatedly calling known-missing UAPI routes (for example 404 fallbacks).
    _uapi_support_cache: dict[tuple[str, int, str, str], bool] = {}

    def __init__(self):
        self.host = get_runtime_setting("WHM_HOST", "")
        self.port = get_runtime_int("WHM_PORT", 2087)
        self.username = get_runtime_setting("WHM_USERNAME", "root")
        self.api_token = get_runtime_setting("WHM_API_TOKEN", "")
        self.base_url = f"https://{self.host}:{self.port}/json-api"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"whm {self.username}:{self.api_token}",
        })
        self.session.verify = True  # Validate SSL in production

    def _call(self, function: str, params: dict = None) -> dict:
        """Make a WHM JSON API call and return the response data."""
        url = f"{self.base_url}/{function}"
        params = params or {}
        params["api.version"] = 1

        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.error(f"WHM API request failed: {e}")
            raise WHMClientError(f"WHM API request failed: {e}") from e

        result = data.get("result", data)
        if isinstance(result, list):
            result = result[0] if result else {}

        if result.get("status") == 0:
            error = result.get("statusmsg", "Unknown WHM error")
            logger.error(f"WHM API error for {function}: {error}")
            raise WHMClientError(f"WHM API error: {error}")

        return data

    def create_account(
        self,
        domain: str,
        username: str,
        password: str,
        package: str,
        email: str,
    ) -> dict:
        """Create a new cPanel hosting account via WHM createacct."""
        params = {
            "domain": domain,
            "username": username,
            "password": password,
            "pkgname": package,
            "contactemail": email,
            "featurelist": "default",
            "ip": "n",
        }
        logger.info(f"Creating cPanel account: username={username}, domain={domain}, package={package}")
        return self._call("createacct", params)

    def suspend_account(self, username: str, reason: str = "") -> dict:
        """Suspend a cPanel account."""
        logger.info(f"Suspending cPanel account: {username}")
        return self._call("suspendacct", {"user": username, "reason": reason})

    def unsuspend_account(self, username: str) -> dict:
        """Unsuspend a cPanel account."""
        logger.info(f"Unsuspending cPanel account: {username}")
        return self._call("unsuspendacct", {"user": username})

    def terminate_account(self, username: str, keep_dns: bool = False) -> dict:
        """Terminate (permanently remove) a cPanel account."""
        logger.warning(f"Terminating cPanel account: {username}")
        return self._call("removeacct", {"user": username, "keepdns": "1" if keep_dns else "0"})

    def change_package(self, username: str, package: str) -> dict:
        """Change the cPanel package for an account."""
        logger.info(f"Changing package for {username} to {package}")
        return self._call("changepackage", {"user": username, "pkg": package})

    def get_account_summary(self, username: str) -> dict:
        """Get summary information about a cPanel account."""
        return self._call("accountsummary", {"user": username})

    def get_disk_usage(self, username: str) -> dict:
        """Get disk usage for a cPanel account."""
        return self._call("showbw", {"searchtype": "user", "search": username})

    def list_accounts(self, columns: list[str] | None = None) -> list:
        """List cPanel accounts, optionally requesting only specific columns."""
        params = None
        if columns:
            params = {f"api.columns.{chr(97 + idx)}": col for idx, col in enumerate(columns)}
            params["api.columns.enable"] = 1

        data = self._call("listaccts", params=params)
        if isinstance(data.get("acct"), list):
            return data.get("acct", [])
        if isinstance(data.get("data"), dict) and isinstance(data["data"].get("acct"), list):
            return data["data"].get("acct", [])
        return []

    def list_users(self) -> list[str]:
        """List WHM users (account usernames)."""
        data = self._call("list_users")
        users = data.get("data", {}).get("users") if isinstance(data.get("data"), dict) else None
        return users if isinstance(users, list) else []

    def modify_account(self, username: str, domain: str = "", contact_email: str = "") -> dict:
        """Modify an existing cPanel account (domain/contact email)."""
        params = {"user": username}
        if domain:
            params["domain"] = domain
        if contact_email:
            params["contactemail"] = contact_email
        return self._call("modifyacct", params)

    def change_password(self, username: str, password: str) -> dict:
        """Change a cPanel account password via WHM passwd endpoint."""
        return self._call("passwd", {"user": username, "password": password})

    def add_zone_record(
        self,
        domain: str,
        name: str,
        record_type: str,
        address: str,
        ttl: int = 3600,
        dns_class: str = "IN",
    ) -> dict:
        """Add a DNS zone record in WHM."""
        return self._call(
            "addzonerecord",
            {
                "domain": domain,
                "name": name,
                "type": record_type,
                "address": address,
                "ttl": ttl,
                "dnsclass": dns_class,
            },
        )

    def edit_zone_record(
        self,
        domain: str,
        line: int,
        name: str,
        record_type: str,
        address: str,
        ttl: int = 3600,
        dns_class: str = "IN",
    ) -> dict:
        """Edit an existing DNS zone record in WHM."""
        return self._call(
            "editzonerecord",
            {
                "domain": domain,
                "line": line,
                "name": name,
                "type": record_type,
                "address": address,
                "ttl": ttl,
                "dnsclass": dns_class,
            },
        )

    def remove_zone_record(self, zone: str, line: int) -> dict:
        """Remove a DNS zone record in WHM."""
        return self._call("removezonerecord", {"zone": zone, "line": line})

    # ── WHM package management ──────────────────────────────────────────────

    def create_package(self, name: str, options: dict | None = None) -> dict:
        """Create a WHM hosting package via addpkg."""
        params = {"name": name}
        if options:
            params.update({k: v for k, v in options.items() if v not in (None, "")})
        return self._call("addpkg", params)

    def update_package(self, name: str, options: dict | None = None) -> dict:
        """Update a WHM hosting package via editpkg."""
        params = {"name": name}
        if options:
            params.update({k: v for k, v in options.items() if v not in (None, "")})
        return self._call("editpkg", params)

    def delete_package(self, name: str) -> dict:
        """Delete a WHM hosting package via killpkg."""
        return self._call("killpkg", {"pkg": name})

    def list_packages(self) -> list:
        """List WHM packages."""
        data = self._call("listpkgs")
        if isinstance(data.get("package"), list):
            return data.get("package", [])
        if isinstance(data.get("pkg"), list):
            return data.get("pkg", [])
        if isinstance(data.get("data"), dict):
            if isinstance(data["data"].get("package"), list):
                return data["data"].get("package", [])
            if isinstance(data["data"].get("pkg"), list):
                return data["data"].get("pkg", [])
        return []

    # ── cPanel UAPI proxy methods ────────────────────────────────────────────
    # WHM can proxy cPanel UAPI calls on behalf of any user via:
    #   GET /execute/{Module}/{function}?cpanel_user={username}

    def _cpanel_call(self, cpanel_username: str, module: str, function: str, params: dict = None) -> dict:
        """Proxy a cPanel UAPI call on behalf of *cpanel_username* via WHM."""
        url = f"https://{self.host}:{self.port}/execute/{module}/{function}"
        query = {"cpanel_user": cpanel_username}
        if params:
            query.update(params)

        try:
            response = self.session.get(url, params=query, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.error(f"cPanel UAPI request failed ({module}/{function}): {e}")
            raise WHMClientError(f"cPanel UAPI request failed: {e}") from e

        if data.get("status") == 0:
            errors = data.get("errors") or ["Unknown cPanel error"]
            logger.error(f"cPanel UAPI error {module}/{function}: {errors}")
            raise WHMClientError(f"cPanel error: {errors[0]}")

        return data

    # ── Email accounts ───────────────────────────────────────────────────────

    def list_email_accounts(self, cpanel_username: str) -> list[dict]:
        """Return all email accounts with disk usage for *cpanel_username*."""
        data = self._cpanel_call(cpanel_username, "Email", "list_pops_with_disk")
        return data.get("data", [])

    def create_email_account(
        self,
        cpanel_username: str,
        email_user: str,
        domain: str,
        password: str,
        quota_mb: int = 500,
    ) -> dict:
        """Create a new email account under *domain* on the cPanel account."""
        logger.info(f"Creating email {email_user}@{domain} for cPanel user {cpanel_username}")
        return self._cpanel_call(
            cpanel_username,
            "Email",
            "add_pop",
            {
                "email": email_user,
                "domain": domain,
                "password": password,
                "quota": quota_mb,
            },
        )

    def delete_email_account(self, cpanel_username: str, email_user: str, domain: str) -> dict:
        """Delete an email account."""
        logger.info(f"Deleting email {email_user}@{domain} for cPanel user {cpanel_username}")
        return self._cpanel_call(
            cpanel_username,
            "Email",
            "delete_pop",
            {"email": email_user, "domain": domain},
        )

    # ── MySQL databases ──────────────────────────────────────────────────────

    def list_databases(self, cpanel_username: str) -> list[dict]:
        """Return all MySQL databases for *cpanel_username*."""
        data = self._cpanel_call(cpanel_username, "Mysql", "list_databases")
        return data.get("data", [])

    def create_database(self, cpanel_username: str, db_name: str) -> dict:
        """Create a MySQL database (name is prefixed with the cPanel username)."""
        logger.info(f"Creating database {db_name} for cPanel user {cpanel_username}")
        return self._cpanel_call(cpanel_username, "Mysql", "create_database", {"name": db_name})

    def delete_database(self, cpanel_username: str, db_name: str) -> dict:
        """Delete a MySQL database."""
        logger.info(f"Deleting database {db_name} for cPanel user {cpanel_username}")
        return self._cpanel_call(cpanel_username, "Mysql", "delete_database", {"name": db_name})

    # ── Disk / quota ─────────────────────────────────────────────────────────

    def get_quota(self, cpanel_username: str) -> dict:
        """Return disk quota info for *cpanel_username* via cPanel UAPI.

        Some servers expose quota via ``Quota/get_quota_info`` while others
        still rely on ``DiskUsage/get_quota``. Try the modern endpoint first,
        then fall back for compatibility.
        """
        errors = []
        for module, function in (("Quota", "get_quota_info"), ("DiskUsage", "get_quota")):
            cache_key = (self.host, self.port, module, function)
            if self._uapi_support_cache.get(cache_key) is False:
                continue
            try:
                data = self._cpanel_call(cpanel_username, module, function)
                self._uapi_support_cache[cache_key] = True
                return data.get("data", {}) if isinstance(data, dict) else {}
            except WHMClientError as exc:
                # If endpoint is missing on this server, mark unsupported to
                # prevent repeated noisy 404 calls on future sync runs.
                if "404" in str(exc):
                    self._uapi_support_cache[cache_key] = False
                errors.append(f"{module}/{function}: {exc}")

        raise WHMClientError(
            "cPanel quota endpoints unavailable: " + " | ".join(errors)
        )

    def create_cpanel_session(self, cpanel_username: str) -> str:
        """Create a WHM-authenticated cPanel session and return the login URL."""
        data = self._call("create_user_session", {"user": cpanel_username, "service": "cpaneld"})
        # WHM returns: {"result": {"url": "https://...", "token": "..."}}
        result = data.get("result") or data
        url = result.get("url", "")
        if not url:
            raise WHMClientError("WHM did not return a session URL.")
        return url


def generate_cpanel_username(domain: str) -> str:
    """Generate a valid 8-char cPanel username from a domain name."""
    base = domain.split(".")[0].lower()
    base = "".join(c for c in base if c.isalnum())[:8]
    if not base or not base[0].isalpha():
        base = "u" + base
    return base[:8]


def generate_secure_password(length: int = 16) -> str:
    """Generate a secure random password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))
