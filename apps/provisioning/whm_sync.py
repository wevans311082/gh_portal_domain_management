"""Synchronization service for persisting WHM/cPanel inventory data."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.utils import timezone

from apps.core.runtime_settings import get_runtime_setting
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
    def _to_bool(value) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}

    @staticmethod
    def _first_non_empty(*values, default=""):
        for value in values:
            if value in (None, ""):
                continue
            return value
        return default

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
            packages = self._extract_list(
                package_payload,
                ("package", "pkg", "data.package", "data.pkg", "data.packages"),
            )
            WHMPackageSnapshot.objects.update(is_active=False)
            for pkg in packages:
                name = str(self._first_non_empty(pkg.get("name"), pkg.get("pkg"), default="")).strip()
                if not name:
                    continue

                WHMPackageSnapshot.objects.update_or_create(
                    name=name,
                    defaults={
                        "owner": str(pkg.get("owner", "") or ""),
                        "feature_list": str(self._first_non_empty(pkg.get("featurelist"), pkg.get("feature_list"), default="")),
                        "disk_quota_mb": str(self._first_non_empty(pkg.get("quota"), pkg.get("diskquota"), default="")),
                        "bandwidth_quota_mb": str(self._first_non_empty(pkg.get("bwlimit"), pkg.get("bandwidth"), default="")),
                        "max_email_accounts": str(self._first_non_empty(pkg.get("maxpop"), pkg.get("max_emailacct_quota"), default="")),
                        "max_ftp_accounts": str(self._first_non_empty(pkg.get("maxftp"), default="")),
                        "max_databases": str(self._first_non_empty(pkg.get("maxsql"), default="")),
                        "max_subdomains": str(self._first_non_empty(pkg.get("maxsub"), default="")),
                        "max_parked_domains": str(self._first_non_empty(pkg.get("maxpark"), default="")),
                        "max_addon_domains": str(self._first_non_empty(pkg.get("maxaddon"), default="")),
                        "payload": pkg,
                        "is_active": True,
                    },
                )

            account_payload = self.client._call("listaccts")
            accounts = self._extract_list(
                account_payload,
                ("acct", "data.acct", "metadata.acct", "result.acct"),
            )

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

                WHMAccountUsageSnapshot.objects.update_or_create(
                    account=account_obj,
                    defaults={
                        "disk_used_mb": str(self._first_non_empty(quota.get("used"), quota.get("disk_used"), default="")),
                        "disk_limit_mb": str(self._first_non_empty(quota.get("limit"), quota.get("disk_limit"), default="")),
                        "disk_used_percent": str(self._first_non_empty(quota.get("percent_used"), quota.get("disk_percent"), default="")),
                        "inode_used": str(self._first_non_empty(quota.get("inode_used"), default="")),
                        "inode_limit": str(self._first_non_empty(quota.get("inode_limit"), default="")),
                        "inode_used_percent": str(self._first_non_empty(quota.get("inode_percent_used"), default="")),
                        "monthly_bandwidth_used_mb": str(self._first_non_empty(showbw.get("bandwidthused"), showbw.get("bwused"), default="")),
                        "monthly_bandwidth_limit_mb": str(self._first_non_empty(showbw.get("bandwidthlimit"), showbw.get("bwlimit"), default="")),
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
