"""WHM/cPanel API client for hosting account provisioning."""
import logging
import secrets
import string
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class WHMClientError(Exception):
    """Raised when WHM API returns an error."""
    pass


class WHMClient:
    """Client for the WHM JSON API v1."""

    def __init__(self):
        self.host = settings.WHM_HOST
        self.port = settings.WHM_PORT
        self.username = settings.WHM_USERNAME
        self.api_token = settings.WHM_API_TOKEN
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

    def list_accounts(self) -> list:
        """List all cPanel accounts."""
        data = self._call("listaccts")
        return data.get("acct", [])

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
        """Return disk quota info for *cpanel_username* via cPanel UAPI."""
        data = self._cpanel_call(cpanel_username, "DiskUsage", "get_quota")
        return data.get("data", {})


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
