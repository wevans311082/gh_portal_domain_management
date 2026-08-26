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

    _POST_FUNCTIONS = frozenset({"createacct", "passwd"})

    def _call(self, function: str, params: dict = None) -> dict:
        """Make a WHM JSON API call and return the response data."""
        url = f"{self.base_url}/{function}"
        params = params or {}
        params["api.version"] = 1

        try:
            if function in self._POST_FUNCTIONS:
                response = self.session.post(url, data=params, timeout=30)
            else:
                response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.error(f"WHM API request failed: {e}")
            raise WHMClientError(f"WHM API request failed: {e}") from e

        metadata = data.get("metadata") if isinstance(data, dict) else None
        if isinstance(metadata, dict) and "result" in metadata:
            if str(metadata.get("result")) in {"0", "false"}:
                error = metadata.get("reason") or "Unknown WHM error"
                logger.error(f"WHM API error for {function}: {error}")
                raise WHMClientError(f"WHM API error: {error}")
            return data

        result = data.get("result", data) if isinstance(data, dict) else data
        if isinstance(result, list):
            result = result[0] if result else {}

        if isinstance(result, dict) and result.get("status") in (0, "0"):
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
            "plan": package,
            "pkgname": package,
            "contactemail": email,
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

    def dump_zone(self, domain: str) -> list[dict]:
        """Return WHM zone records for *domain* (best-effort parse)."""
        data = self._call("dumpzone", {"domain": domain})
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        records: list[dict] = []
        zone_blocks = []
        if isinstance(payload, dict):
            zone_blocks = payload.get("zone") or payload.get("zones") or []
        if isinstance(payload, list):
            zone_blocks = payload
        if not isinstance(zone_blocks, list):
            zone_blocks = [zone_blocks] if zone_blocks else []
        for block in zone_blocks:
            items = block
            if isinstance(block, dict):
                items = block.get("record") or block.get("records") or []
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    records.append(item)
        return records

    def modify_account_nameservers(self, username: str, nameservers: list[str]) -> dict:
        """Best-effort update of account nameserver fields via modifyacct."""
        params = {"user": username}
        for idx, host in enumerate(nameservers[:4], start=1):
            host = str(host or "").strip()
            if host:
                params[f"ns{idx}"] = host
        return self._call("modifyacct", params)

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

    def get_nameservers(self) -> list[str]:
        """Best-effort list of authoritative nameservers from WHM.

        Tries modern nameserver config APIs, then falls back to ``ns1.``/``ns2.``
        derived from the server hostname when WHM is configured.
        """
        names: list[str] = []

        for function in ("get_nameserver_config", "nameserver_config", "list_nameservers"):
            try:
                data = self._call(function)
            except WHMClientError:
                continue
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            if not isinstance(payload, dict):
                continue
            # Common key shapes across WHM versions
            candidates = []
            for key in (
                "nameserver",
                "nameservers",
                "ns",
                "nameserver_list",
            ):
                val = payload.get(key)
                if isinstance(val, list):
                    candidates.extend(val)
                elif isinstance(val, str) and val.strip():
                    candidates.append(val)
            for i in range(1, 5):
                for key in (f"nameserver{i}", f"ns{i}", f"nameserver_{i}"):
                    val = payload.get(key)
                    if val:
                        candidates.append(val)
            for item in candidates:
                if isinstance(item, dict):
                    host = (
                        item.get("nameserver")
                        or item.get("name")
                        or item.get("hostname")
                        or item.get("ns")
                        or ""
                    )
                else:
                    host = str(item or "")
                host = host.strip().lower().rstrip(".")
                if host and host not in names:
                    names.append(host)
            if len(names) >= 2:
                return names[:4]

        # Fallback: derive ns1/ns2 from WHM hostname (common cPanel convention)
        try:
            data = self._call("gethostname")
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            hostname = ""
            if isinstance(payload, dict):
                hostname = str(payload.get("hostname") or payload.get("host") or "").strip().lower()
            if not hostname and self.host:
                hostname = str(self.host).strip().lower()
            hostname = hostname.rstrip(".")
            if hostname and "." in hostname:
                # Prefer apex for ns labels when host is server.example.com → example.com
                parts = hostname.split(".")
                apex = ".".join(parts[-2:]) if len(parts) >= 2 else hostname
                # Prefer ns under the full hostname domain first
                derived = [f"ns1.{apex}", f"ns2.{apex}"]
                return derived
        except WHMClientError as exc:
            logger.warning("WHM gethostname for nameservers failed: %s", exc)

        return names

    # ── cPanel UAPI / API2 proxy via WHM json-api/cpanel ─────────────────────
    # Cadence/cPanel WHM on :2087 does NOT serve /execute/Module/function
    # (that path is the cPanel port 2083 UAPI). From WHM we must impersonate:
    #   GET /json-api/cpanel?cpanel_jsonapi_user=...&cpanel_jsonapi_apiversion=3
    #   &cpanel_jsonapi_module=Email&cpanel_jsonapi_func=list_pops_with_disk

    _API2_FUNC_ALIASES = {
        ("Email", "list_pops_with_disk"): ("Email", "listpopswithdisk"),
        ("Email", "list_pops"): ("Email", "listpops"),
        ("Email", "add_pop"): ("Email", "addpop"),
        ("Email", "delete_pop"): ("Email", "delpop"),
        ("Mysql", "list_databases"): ("MysqlFE", "listdbs"),
        ("Quota", "get_quota_info"): ("DiskUsage", "fetchdiskusage"),
        ("DiskUsage", "get_quota"): ("DiskUsage", "fetchdiskusage"),
    }

    @staticmethod
    def _unwrap_cpanel_payload(data: dict):
        """Normalize WHM-proxied UAPI and API2 payloads to a data value."""
        if not isinstance(data, dict):
            return data
        if data.get("status") == 0 and data.get("data") is None:
            errors = data.get("errors") or ["Unknown cPanel error"]
            raise WHMClientError(f"cPanel error: {errors[0]}")
        if "cpanelresult" in data and isinstance(data["cpanelresult"], dict):
            result = data["cpanelresult"]
            event = result.get("event") if isinstance(result.get("event"), dict) else {}
            if str(event.get("result")) in {"0", "false"}:
                raise WHMClientError(result.get("error") or "cPanel API2 call failed")
            payload = result.get("data")
            return payload if payload is not None else result
        inner = data.get("data")
        if isinstance(inner, dict) and "result" in inner:
            result = inner["result"]
            if isinstance(result, dict):
                if str(result.get("status")) in {"0", "false"}:
                    errors = result.get("errors") or ["Unknown cPanel error"]
                    raise WHMClientError(f"cPanel error: {errors[0]}")
                return result.get("data")
            return result
        if isinstance(inner, dict) and "data" in inner:
            return inner.get("data")
        if inner is not None:
            return inner
        return data.get("data", data)

    def _cpanel_call(self, cpanel_username: str, module: str, function: str, params: dict = None) -> dict:
        """Proxy a cPanel UAPI/API2 call via WHM ``json-api/cpanel``."""
        attempts = [(3, module, function)]
        alias = self._API2_FUNC_ALIASES.get((module, function))
        if alias:
            attempts.append((2, alias[0], alias[1]))
        attempts.append((2, module, function.replace("_", "")))

        errors: list[str] = []
        seen: set[tuple] = set()
        for api_version, mod, func in attempts:
            key = (api_version, mod, func)
            if key in seen:
                continue
            seen.add(key)
            query = {
                "cpanel_jsonapi_user": cpanel_username,
                "cpanel_jsonapi_apiversion": api_version,
                "cpanel_jsonapi_module": mod,
                "cpanel_jsonapi_func": func,
            }
            if params:
                query.update(params)
            try:
                data = self._call("cpanel", query)
                payload = self._unwrap_cpanel_payload(data)
                return {"data": payload, "status": 1, "raw": data}
            except WHMClientError as exc:
                errors.append(f"{mod}/{func} (v{api_version}): {exc}")
                continue

        raise WHMClientError(
            "cPanel call failed via WHM json-api/cpanel: " + " | ".join(errors)
        )

    # ── Email accounts ───────────────────────────────────────────────────────

    def list_email_accounts(self, cpanel_username: str) -> list[dict]:
        """Return all email accounts with disk usage for *cpanel_username*."""
        data = self._cpanel_call(cpanel_username, "Email", "list_pops_with_disk")
        payload = data.get("data", []) if isinstance(data, dict) else data
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("pops") or []
        return payload if isinstance(payload, list) else []

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
        payload = data.get("data", []) if isinstance(data, dict) else data
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("databases") or []
        return payload if isinstance(payload, list) else []

    def create_database(self, cpanel_username: str, db_name: str) -> dict:
        """Create a MySQL database (name is prefixed with the cPanel username)."""
        logger.info(f"Creating database {db_name} for cPanel user {cpanel_username}")
        return self._cpanel_call(cpanel_username, "Mysql", "create_database", {"name": db_name})

    def delete_database(self, cpanel_username: str, db_name: str) -> dict:
        """Delete a MySQL database."""
        logger.info(f"Deleting database {db_name} for cPanel user {cpanel_username}")
        return self._cpanel_call(cpanel_username, "Mysql", "delete_database", {"name": db_name})

    # ── Disk / quota ─────────────────────────────────────────────────────────

    @staticmethod
    def _size_to_mb(value) -> float:
        """Parse WHM sizes such as ``152``, ``152.3M``, ``1.2G``, ``unlimited``."""
        text = str(value or "").strip().lower().replace(",", "").replace(" ", "")
        if text in {"", "unlimited", "none", "null", "n/a", "na"}:
            return 0.0
        multiplier = 1.0
        if text.endswith("t"):
            multiplier = 1024 * 1024
            text = text[:-1]
        elif text.endswith("g"):
            multiplier = 1024.0
            text = text[:-1]
        elif text.endswith("m"):
            multiplier = 1.0
            text = text[:-1]
        elif text.endswith("k"):
            multiplier = 1.0 / 1024.0
            text = text[:-1]
        elif text.endswith("b"):
            multiplier = 1.0 / (1024.0 * 1024.0)
            text = text[:-1]
        try:
            return float(text) * multiplier
        except (TypeError, ValueError):
            return 0.0

    def _account_summary_record(self, cpanel_username: str) -> dict:
        summary = self.get_account_summary(cpanel_username)
        payload = summary.get("data") if isinstance(summary.get("data"), dict) else summary
        acct = payload.get("acct") if isinstance(payload, dict) else None
        if isinstance(acct, list) and acct and isinstance(acct[0], dict):
            return acct[0]
        if isinstance(acct, dict):
            return acct
        return payload if isinstance(payload, dict) else {}

    def get_quota(self, cpanel_username: str) -> dict:
        """Return disk quota for *cpanel_username*.

        Prefer WHM ``accountsummary`` (API 1 on :2087) because Cadence WHM
        does not expose ``/execute/Quota/...``. Fall back to proxied UAPI.
        """
        try:
            summary = self._account_summary_record(cpanel_username)
            used = summary.get("diskused") or summary.get("disk_used") or summary.get("diskusage")
            limit = summary.get("disklimit") or summary.get("disk_limit") or summary.get("diskquota")
            if used not in (None, ""):
                used_mb = self._size_to_mb(used)
                limit_mb = self._size_to_mb(limit)
                return {
                    "megabytes_used": used_mb,
                    "megabytes_limit": limit_mb,
                    "bytesused": int(used_mb * 1024 * 1024),
                    "bytelimit": int(limit_mb * 1024 * 1024) if limit_mb else "unlimited",
                    "source": "accountsummary",
                }
        except WHMClientError as exc:
            logger.info("accountsummary quota lookup failed for %s: %s", cpanel_username, exc)

        errors = []
        for module, function in (("Quota", "get_quota_info"), ("DiskUsage", "get_quota")):
            cache_key = (self.host, self.port, module, function)
            if self._uapi_support_cache.get(cache_key) is False:
                continue
            try:
                data = self._cpanel_call(cpanel_username, module, function)
                self._uapi_support_cache[cache_key] = True
                payload = data.get("data", {}) if isinstance(data, dict) else {}
                if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                    payload = payload[0]
                return payload if isinstance(payload, dict) else {}
            except WHMClientError as exc:
                if "404" in str(exc):
                    self._uapi_support_cache[cache_key] = False
                errors.append(f"{module}/{function}: {exc}")

        raise WHMClientError(
            "cPanel quota endpoints unavailable: " + " | ".join(errors)
        )

    def create_cpanel_session(self, cpanel_username: str) -> str:
        """Create a WHM-authenticated cPanel session and return the login URL."""
        data = self._call("create_user_session", {"user": cpanel_username, "service": "cpaneld"})
        url = ""
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        url = payload.get("url") or ""
        if not url:
            result = data.get("result") if isinstance(data.get("result"), dict) else data
            if isinstance(result, dict):
                url = result.get("url") or ""
                nested = result.get("data") if isinstance(result.get("data"), dict) else {}
                url = url or nested.get("url") or ""
        if not url:
            raise WHMClientError("WHM did not return a session URL.")
        return url

    def collect_account_stats(self, cpanel_username: str) -> dict:
        """Gather disk, bandwidth, email, and database stats for a cPanel user."""
        stats = {
            "quota": {},
            "summary": {},
            "bandwidth": {},
            "emails": [],
            "databases": [],
            "errors": [],
        }
        try:
            stats["summary"] = self._account_summary_record(cpanel_username) or {}
        except WHMClientError as exc:
            stats["errors"].append(f"Account summary: {exc}")
        try:
            stats["quota"] = self.get_quota(cpanel_username) or {}
        except WHMClientError as exc:
            if not stats["summary"]:
                stats["errors"].append(f"Quota: {exc}")
        try:
            bw = self.get_disk_usage(cpanel_username)
            payload = bw.get("data") if isinstance(bw.get("data"), dict) else bw
            acct = payload.get("acct") if isinstance(payload, dict) else None
            if isinstance(acct, list) and acct:
                stats["bandwidth"] = acct[0] if isinstance(acct[0], dict) else {}
            elif isinstance(acct, dict):
                stats["bandwidth"] = acct
            elif isinstance(payload, dict):
                stats["bandwidth"] = payload
        except WHMClientError as exc:
            stats["errors"].append(f"Bandwidth: {exc}")
        try:
            emails = self.list_email_accounts(cpanel_username) or []
            if isinstance(emails, dict):
                emails = emails.get("data") or emails.get("pops") or []
            stats["emails"] = emails if isinstance(emails, list) else []
        except WHMClientError as exc:
            logger.info("Email list unavailable for %s: %s", cpanel_username, exc)
        try:
            databases = self.list_databases(cpanel_username) or []
            if isinstance(databases, dict):
                databases = databases.get("data") or []
            stats["databases"] = databases if isinstance(databases, list) else []
        except WHMClientError as exc:
            logger.info("Database list unavailable for %s: %s", cpanel_username, exc)
        return stats


def sync_user_cpanel_passwords(user, password: str) -> tuple[int, list[str]]:
    """Update cPanel passwords for all active hosting services owned by *user*."""
    from apps.services.models import Service

    updated = 0
    errors: list[str] = []
    services = Service.objects.filter(user=user, status=Service.STATUS_ACTIVE).exclude(cpanel_username="")
    if not services.exists():
        return 0, []
    client = WHMClient()
    for service in services:
        try:
            client.change_password(service.cpanel_username, password)
            updated += 1
        except WHMClientError as exc:
            errors.append(f"{service.cpanel_username}: {exc}")
    return updated, errors


def generate_cpanel_username(domain: str, unique_suffix: str = "") -> str:
    """Generate a valid cPanel username (letter-start, max 16 chars)."""
    label = (domain or "").split(".")[0].lower()
    base = "".join(c for c in label if c.isalnum())
    suffix = "".join(c for c in str(unique_suffix) if c.isalnum())[-4:]
    if suffix:
        base = f"{base[:12]}{suffix}"
    else:
        base = base[:16]
    if not base or not base[0].isalpha():
        base = "u" + base
    return base[:16]


def generate_secure_password(length: int = 16) -> str:
    """Generate a secure random password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))
