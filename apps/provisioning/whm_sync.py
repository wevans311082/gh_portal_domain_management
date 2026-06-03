"""Synchronization service for persisting WHM/cPanel inventory data."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.db.models import Q
from django.utils import timezone

from apps.core.runtime_settings import get_runtime_setting
from apps.domains.models import Domain
from apps.domains.resellerclub_client import ResellerClubClient
from apps.services.models import Service
from apps.provisioning.models import (
    WHMAccountSnapshot,
    WHMAccountUsageSnapshot,
    WHMPackageSnapshot,
    WHMServerSnapshot,
    WHMSyncRun,
)
from apps.provisioning.whm_client import WHMClient, WHMClientError

logger = logging.getLogger(__name__)


class WHMSyncService:
    """Fetch account/package/usage data from WHM and store snapshots locally."""

    def __init__(self, client: WHMClient | None = None):
        self.client = client or WHMClient()

    @staticmethod
    def _extract_list(payload: dict, candidates: Iterable[str]) -> list[dict]:
        """Extract a list from nested API payloads across common WHM shapes."""
        for key in candidates:
            current = payload
            try:
                for part in key.split("."):
                    current = current[part]
            except Exception:
                continue
            if isinstance(current, list):
                return current
        return []

    @staticmethod
    def _extract_mapping(payload: dict, candidates: Iterable[str]) -> dict:
        for key in candidates:
            current = payload
            try:
                for part in key.split("."):
                    current = current[part]
            except Exception:
                continue
            if isinstance(current, dict):
                return current
        return {}

    def _extract_packages(self, payload: dict) -> list[dict]:
        packages = self._extract_list(payload, ("package", "pkg", "data.package", "data.pkg", "data.packages"))
        if packages:
            return [pkg for pkg in packages if isinstance(pkg, dict)]

        package_map = self._extract_mapping(payload, ("data.pkg", "data.package", "pkg", "package"))
        records = []
        for name, details in package_map.items():
            if isinstance(details, dict):
                record = dict(details)
                record.setdefault("name", name)
                records.append(record)
        return records

    def _extract_accounts(self, payload: dict) -> list[dict]:
        accounts = self._extract_list(payload, ("acct", "data.acct", "metadata.acct", "result.acct"))
        if accounts:
            return [acct for acct in accounts if isinstance(acct, dict)]

        account_map = self._extract_mapping(payload, ("data.acct", "acct"))
        records = []
        for username, details in account_map.items():
            if isinstance(details, dict):
                record = dict(details)
                record.setdefault("user", username)
                records.append(record)
        return records

    def _extract_accountsummary(self, payload: dict) -> dict:
        items = self._extract_list(payload, ("data.acct", "acct", "data.account", "account"))
        if items:
            first_item = items[0]
            if isinstance(first_item, dict):
                return first_item
        mapping = self._extract_mapping(payload, ("data", "metadata", "result"))
        return mapping if isinstance(mapping, dict) else {}

    @staticmethod
    def _flatten_for_lookup(payload):
        if isinstance(payload, dict):
            yield payload
            for value in payload.values():
                yield from WHMSyncService._flatten_for_lookup(value)
        elif isinstance(payload, list):
            for item in payload:
                yield from WHMSyncService._flatten_for_lookup(item)

    def _pick_value(self, payload, *keys, default=""):
        for node in self._flatten_for_lookup(payload):
            for key in keys:
                if key in node and node[key] not in (None, ""):
                    return node[key]
        return default

    @staticmethod
    def _to_bool(value) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}

    @staticmethod
    def _first_non_empty(*values, default=""):
        for value in values:
            if value in (None, ""):
                continue
            return value
        return default

    @staticmethod
    def _bytes_to_mb(value) -> str:
        try:
            raw = float(str(value).strip())
        except Exception:
            return ""
        if raw < 0:
            return ""
        return str(round(raw / (1024 * 1024), 2))

    @staticmethod
    def normalize_domain(value: str) -> str:
        return str(value or "").strip().lower().rstrip(".")

    @staticmethod
    def _registrar_status_is_active(value: str) -> bool:
        # LogicBoxes uses "Active" for live registered domains. Avoid matching
        # "InActive", which still contains the word "active".
        return str(value or "").strip().lower() == "active"

    def build_domain_reconciliation(
        self,
        registrar_orders: list[dict] | None = None,
        include_registrar_details: bool = False,
        max_registrar_details: int = 100,
    ) -> dict:
        """Compare active ResellerClub domains with active WHM accounts.

        ResellerClub is treated as the source of truth for whether a domain is
        registered and active. WHM accounts whose primary domain is not active
        at the registrar are returned as orphan/stale candidates for manual
        review and optional termination.
        """
        if registrar_orders is None:
            registrar_orders = ResellerClubClient().list_all_domain_orders(
                no_of_records=100,
                status="All",
                include_details=include_registrar_details,
                max_details=max_registrar_details,
                max_pages=50,
            )

        registrar_by_domain: dict[str, dict] = {}
        active_registrar_domains: set[str] = set()
        for order in registrar_orders or []:
            if not isinstance(order, dict):
                continue
            domain_name = self.normalize_domain(ResellerClubClient._extract_domain_name_from_order(order))
            if not domain_name:
                continue
            order.setdefault("domainname", domain_name)
            extracted_status = ResellerClubClient._extract_status_from_order(order)
            if extracted_status and not order.get("currentstatus"):
                order["currentstatus"] = extracted_status
            extracted_order_id = ResellerClubClient._extract_order_id_from_order(order)
            if extracted_order_id and not order.get("orderid"):
                order["orderid"] = extracted_order_id
            registrar_by_domain[domain_name] = order
            if self._registrar_status_is_active(order.get("currentstatus")):
                active_registrar_domains.add(domain_name)

        active_accounts = list(
            WHMAccountSnapshot.objects.filter(is_active=True)
            .select_related("service", "service__user")
            .order_by("username")
        )
        whm_domains: set[str] = set()
        matched_accounts = []
        orphaned_accounts = []
        for account in active_accounts:
            account_domain = self.normalize_domain(account.domain)
            service_domain = self.normalize_domain(account.service.domain_name if account.service else "")
            comparison_domain = account_domain or service_domain
            if comparison_domain:
                whm_domains.add(comparison_domain)

            registrar_order = registrar_by_domain.get(comparison_domain)
            row = {
                "account": account,
                "username": account.username,
                "domain": comparison_domain,
                "registrar_order": registrar_order,
                "registrar_status": (registrar_order or {}).get("currentstatus", ""),
                "reason": "",
                "service_id": account.service_id,
                "suspended": account.suspended,
                "plan": account.plan,
            }

            if comparison_domain and comparison_domain in active_registrar_domains:
                matched_accounts.append(row)
                continue

            if not comparison_domain:
                row["reason"] = "No domain recorded on the WHM account or linked service."
            elif registrar_order:
                row["reason"] = f"Registrar status is {(registrar_order.get('currentstatus') or 'unknown')}."
            else:
                row["reason"] = "Domain is not present in the registrar inventory."
            orphaned_accounts.append(row)

        registrar_only_domains = []
        for domain_name in sorted(active_registrar_domains - whm_domains):
            registrar_only_domains.append(
                {
                    "domain": domain_name,
                    "registrar_order": registrar_by_domain.get(domain_name),
                    "local_domain": Domain.objects.filter(name__iexact=domain_name).first(),
                    "service": Service.objects.filter(
                        Q(domain_name__iexact=domain_name) | Q(cpanel_domain__iexact=domain_name)
                    ).first(),
                }
            )

        local_active_domains = {
            self.normalize_domain(domain.name): domain
            for domain in Domain.objects.filter(status=Domain.STATUS_ACTIVE)
        }
        local_stale_domains = [
            {
                "domain": domain_name,
                "local_domain": local_domain,
                "registrar_order": registrar_by_domain.get(domain_name),
                "registrar_status": (registrar_by_domain.get(domain_name) or {}).get("currentstatus", ""),
            }
            for domain_name, local_domain in sorted(local_active_domains.items())
            if domain_name and domain_name not in active_registrar_domains
        ]

        return {
            "registrar_domain_total": len(registrar_by_domain),
            "active_registrar_domain_total": len(active_registrar_domains),
            "whm_account_total": len(active_accounts),
            "matched_account_total": len(matched_accounts),
            "orphaned_account_total": len(orphaned_accounts),
            "registrar_only_domain_total": len(registrar_only_domains),
            "local_stale_domain_total": len(local_stale_domains),
            "matched_accounts": matched_accounts[:100],
            "orphaned_accounts": orphaned_accounts[:100],
            "registrar_only_domains": registrar_only_domains[:100],
            "local_stale_domains": local_stale_domains[:100],
        }

    @staticmethod
    def serialize_domain_reconciliation(report: dict) -> dict:
        """Convert a reconciliation report into JSON-safe data for storage."""
        def registrar_summary(order):
            if not isinstance(order, dict):
                return {}
            return {
                "domainname": order.get("domainname", ""),
                "currentstatus": order.get("currentstatus", ""),
                "orderid": order.get("orderid", ""),
                "expiry_date": order.get("expiry_date", ""),
            }

        def account_row(row):
            account = row.get("account")
            return {
                "username": row.get("username") or getattr(account, "username", ""),
                "domain": row.get("domain", ""),
                "registrar_status": row.get("registrar_status", ""),
                "reason": row.get("reason", ""),
                "service_id": row.get("service_id") or getattr(account, "service_id", None),
                "suspended": bool(row.get("suspended", getattr(account, "suspended", False))),
                "plan": row.get("plan") or getattr(account, "plan", ""),
                "registrar_order": registrar_summary(row.get("registrar_order")),
            }

        def registrar_only_row(row):
            local_domain = row.get("local_domain")
            service = row.get("service")
            return {
                "domain": row.get("domain", ""),
                "registrar_order": registrar_summary(row.get("registrar_order")),
                "local_domain_id": getattr(local_domain, "id", None),
                "service_id": getattr(service, "id", None),
            }

        def local_stale_row(row):
            local_domain = row.get("local_domain")
            return {
                "domain": row.get("domain", ""),
                "local_domain_id": getattr(local_domain, "id", None),
                "registrar_status": row.get("registrar_status", ""),
                "registrar_order": registrar_summary(row.get("registrar_order")),
            }

        return {
            "generated_at": timezone.now().isoformat(),
            "registrar_domain_total": report.get("registrar_domain_total", 0),
            "active_registrar_domain_total": report.get("active_registrar_domain_total", 0),
            "whm_account_total": report.get("whm_account_total", 0),
            "matched_account_total": report.get("matched_account_total", 0),
            "orphaned_account_total": report.get("orphaned_account_total", 0),
            "registrar_only_domain_total": report.get("registrar_only_domain_total", 0),
            "local_stale_domain_total": report.get("local_stale_domain_total", 0),
            "matched_accounts": [account_row(row) for row in report.get("matched_accounts", [])],
            "orphaned_accounts": [account_row(row) for row in report.get("orphaned_accounts", [])],
            "registrar_only_domains": [registrar_only_row(row) for row in report.get("registrar_only_domains", [])],
            "local_stale_domains": [local_stale_row(row) for row in report.get("local_stale_domains", [])],
        }

    def terminate_orphaned_account(self, username: str, keep_dns: bool = False) -> dict:
        """Terminate a WHM account only if reconciliation still marks it orphaned."""
        normalized_username = str(username or "").strip()
        if not normalized_username:
            raise ValueError("WHM username is required.")

        report = self.build_domain_reconciliation()
        orphaned_usernames = {
            row["account"].username
            for row in report.get("orphaned_accounts", [])
            if row.get("account")
        }
        if normalized_username not in orphaned_usernames:
            raise ValueError(f"{normalized_username} is not currently flagged as a registrar orphan.")

        result = self.client.terminate_account(normalized_username, keep_dns=keep_dns)
        WHMAccountSnapshot.objects.filter(username__iexact=normalized_username).update(
            is_active=False,
            payload={
                "terminated_by_reconciliation": True,
                "terminated_at": timezone.now().isoformat(),
                "keep_dns": keep_dns,
                "whm_response": result,
            },
        )
        Service.objects.filter(cpanel_username__iexact=normalized_username).update(
            status=Service.STATUS_TERMINATED,
            whm_last_sync_action="terminate_orphan",
            whm_last_sync_at=timezone.now(),
            whm_last_sync_ok=True,
            whm_last_sync_message="Terminated after ResellerClub/WHM reconciliation.",
        )
        return result

    def sync_all(self) -> dict:
        sync_run = WHMSyncRun.objects.create(status=WHMSyncRun.STATUS_RUNNING)
        errors: list[str] = []

        try:
            host = get_runtime_setting("WHM_HOST", "") or ""
            version_payload = self.client._call("version")
            server_version = str(
                self._first_non_empty(
                    version_payload.get("version"),
                    (version_payload.get("data") or {}).get("version"),
                    default="",
                )
            )
            WHMServerSnapshot.objects.create(
                host=str(host),
                server_version=server_version,
                payload=version_payload,
            )

            package_payload = self.client._call("listpkgs")
            packages = self._extract_packages(package_payload)
            WHMPackageSnapshot.objects.update(is_active=False)
            for pkg in packages:
                name = str(self._first_non_empty(pkg.get("name"), pkg.get("pkg"), default="")).strip()
                if not name:
                    continue

                WHMPackageSnapshot.objects.update_or_create(
                    name=name,
                    defaults={
                        "owner": str(self._pick_value(pkg, "owner", "reseller", "OWNER", "CREATOR", default="")),
                        "feature_list": str(self._pick_value(pkg, "featurelist", "feature_list", "featurelistname", "FEATURELIST", default="")),
                        "disk_quota_mb": str(self._pick_value(pkg, "quota", "diskquota", "disk_limit", "QUOTA", default="")),
                        "bandwidth_quota_mb": str(self._pick_value(pkg, "bwlimit", "bandwidth", "bandwidth_limit", "BWLIMIT", "MAXBW", default="")),
                        "max_email_accounts": str(self._pick_value(pkg, "maxpop", "max_emailacct_quota", "max_email_accounts", "MAXPOP", default="")),
                        "max_ftp_accounts": str(self._pick_value(pkg, "maxftp", "MAXFTP", default="")),
                        "max_databases": str(self._pick_value(pkg, "maxsql", "MAXSQL", default="")),
                        "max_subdomains": str(self._pick_value(pkg, "maxsub", "MAXSUB", default="")),
                        "max_parked_domains": str(self._pick_value(pkg, "maxpark", "MAXPARK", default="")),
                        "max_addon_domains": str(self._pick_value(pkg, "maxaddon", "MAXADDON", default="")),
                        "payload": pkg,
                        "is_active": True,
                    },
                )

            try:
                accounts = self.client.list_accounts(
                    columns=[
                        "user",
                        "domain",
                        "email",
                        "owner",
                        "plan",
                        "ip",
                        "partition",
                        "unix_startdate",
                        "suspended",
                        "suspendreason",
                        "diskused",
                        "disklimit",
                        "bandwidthused",
                        "maxbw",
                    ]
                )
            except Exception:
                account_payload = self.client._call("listaccts")
                accounts = self._extract_accounts(account_payload)

            WHMAccountSnapshot.objects.update(is_active=False)
            for acct in accounts:
                username = str(self._first_non_empty(acct.get("user"), acct.get("username"), default="")).strip()
                if not username:
                    continue

                service = Service.objects.filter(cpanel_username__iexact=username).first()
                account_obj, _ = WHMAccountSnapshot.objects.update_or_create(
                    username=username,
                    defaults={
                        "domain": str(acct.get("domain", "") or ""),
                        "email": str(acct.get("email", "") or ""),
                        "owner": str(acct.get("owner", "") or ""),
                        "plan": str(self._first_non_empty(acct.get("plan"), acct.get("package"), default="")),
                        "ip": str(self._first_non_empty(acct.get("ip"), acct.get("ipv4"), default="")),
                        "server": str(acct.get("server", "") or ""),
                        "partition": str(acct.get("partition", "") or ""),
                        "unix_start_date": str(self._first_non_empty(acct.get("unix_startdate"), acct.get("startdate"), default="")),
                        "suspended": self._to_bool(self._first_non_empty(acct.get("suspended"), acct.get("is_locked"), default=False)),
                        "suspended_reason": str(self._first_non_empty(acct.get("suspendreason"), acct.get("suspend_reason"), default="")),
                        "service": service,
                        "payload": acct,
                        "is_active": True,
                    },
                )

                usage_payload: dict = {}
                try:
                    summary_payload = self.client.get_account_summary(username)
                    usage_payload["accountsummary"] = summary_payload
                except Exception as exc:  # pragma: no cover - best effort on remote API
                    errors.append(f"accountsummary({username}): {exc}")

                try:
                    bw_payload = self.client.get_disk_usage(username)
                    usage_payload["showbw"] = bw_payload
                except Exception as exc:  # pragma: no cover - best effort on remote API
                    errors.append(f"showbw({username}): {exc}")

                try:
                    quota_payload = self.client.get_quota(username)
                    usage_payload["quota"] = quota_payload
                except Exception as exc:  # pragma: no cover - best effort on remote API
                    errors.append(f"quota({username}): {exc}")

                quota = usage_payload.get("quota") or {}
                showbw = usage_payload.get("showbw") or {}
                summary = self._extract_accountsummary(usage_payload.get("accountsummary") or {})

                WHMAccountUsageSnapshot.objects.update_or_create(
                    account=account_obj,
                    defaults={
                        "disk_used_mb": str(self._first_non_empty(
                            self._pick_value(quota, "used", "disk_used", "usage", default=""),
                            self._bytes_to_mb(self._pick_value(quota, "used_bytes", "usedbytes", default="")),
                            self._pick_value(summary, "diskused", "disk_used", default=""),
                            self._pick_value(acct, "diskused", "disk_used", default=""),
                            default="",
                        )),
                        "disk_limit_mb": str(self._first_non_empty(
                            self._pick_value(quota, "limit", "disk_limit", default=""),
                            self._bytes_to_mb(self._pick_value(quota, "limit_bytes", "limitbytes", default="")),
                            self._pick_value(summary, "disklimit", "disk_limit", default=""),
                            self._pick_value(acct, "disklimit", "disk_limit", default=""),
                            default="",
                        )),
                        "disk_used_percent": str(self._first_non_empty(
                            self._pick_value(quota, "percent_used", "disk_percent", default=""),
                            self._pick_value(summary, "diskusedpercent", default=""),
                            default="",
                        )),
                        "inode_used": str(self._pick_value(quota, "inode_used", "file_usage", default="")),
                        "inode_limit": str(self._pick_value(quota, "inode_limit", "file_limit", default="")),
                        "inode_used_percent": str(self._pick_value(quota, "inode_percent_used", "file_usage_percent", default="")),
                        "monthly_bandwidth_used_mb": str(self._first_non_empty(
                            self._pick_value(showbw, "bandwidthused", "bwused", default=""),
                            self._bytes_to_mb(self._pick_value(showbw, "totalbytes", "total_bytes", default="")),
                            self._pick_value(summary, "bandwidthused", default=""),
                            self._pick_value(acct, "bandwidthused", "bwused", default=""),
                            default="",
                        )),
                        "monthly_bandwidth_limit_mb": str(self._first_non_empty(
                            self._pick_value(showbw, "bandwidthlimit", "bwlimit", default=""),
                            self._bytes_to_mb(self._pick_value(showbw, "bandwidthlimitbytes", "bwlimitbytes", default="")),
                            self._pick_value(summary, "maxbw", default=""),
                            self._pick_value(acct, "maxbw", default=""),
                            default="",
                        )),
                        "payload": usage_payload,
                    },
                )

            sync_run.status = WHMSyncRun.STATUS_COMPLETED
            sync_run.package_count = WHMPackageSnapshot.objects.filter(is_active=True).count()
            sync_run.account_count = WHMAccountSnapshot.objects.filter(is_active=True).count()
            sync_run.usage_count = WHMAccountUsageSnapshot.objects.count()
            sync_run.error_count = len(errors)
            sync_run.result_data = {
                "server_version": server_version,
                "packages_seen": len(packages),
                "accounts_seen": len(accounts),
                "errors": errors[:100],
            }
            sync_run.finished_at = timezone.now()
            sync_run.save(
                update_fields=[
                    "status",
                    "package_count",
                    "account_count",
                    "usage_count",
                    "error_count",
                    "result_data",
                    "finished_at",
                    "updated_at",
                ]
            )

            return {
                "ok": True,
                "sync_run_id": sync_run.pk,
                "package_count": sync_run.package_count,
                "account_count": sync_run.account_count,
                "usage_count": sync_run.usage_count,
                "error_count": sync_run.error_count,
            }
        except WHMClientError as exc:
            logger.error("WHM inventory sync failed: %s", exc)
            sync_run.status = WHMSyncRun.STATUS_FAILED
            sync_run.last_error = str(exc)
            sync_run.error_count = max(len(errors), 1)
            sync_run.result_data = {"errors": errors[:100]}
            sync_run.finished_at = timezone.now()
            sync_run.save(
                update_fields=["status", "last_error", "error_count", "result_data", "finished_at", "updated_at"]
            )
            raise
