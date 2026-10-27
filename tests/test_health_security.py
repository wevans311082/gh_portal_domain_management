"""SEC-09: stable anonymous health contract and private diagnostics."""
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.core import health

pytestmark = pytest.mark.django_db

SENSITIVE_ERROR = "postgres://admin:secret-password@private-db:5432/customer_database"


def test_healthy_anonymous_contract_uses_real_database_and_cache(client):
    cache.clear()
    response = client.get(reverse("core:health_check"))
    assert response.status_code == 200
    data = response.json()
    assert {key: data[key] for key in ("status", "database", "cache")} == {
        "status": "ok", "database": "ok", "cache": "ok",
    }
    assert UUID(data["correlation_id"]).hex == data["correlation_id"]
    assert response["X-Correlation-ID"] == data["correlation_id"]
    assert response["Cache-Control"] == "no-store"
    assert cache.get(f"health-probe:{data['correlation_id']}") is None


@pytest.mark.parametrize("failed", ["database", "cache", "both"])
def test_failures_are_generic_and_diagnostics_are_correlated(client, monkeypatch, caplog, failed):
    # The application logger deliberately does not propagate to pytest's root
    # handler. Capture its real LogRecords without changing production logging.
    monkeypatch.setattr(health.logger, "handlers", [*health.logger.handlers, caplog.handler])
    if failed in {"database", "both"}:
        database = MagicMock()
        database.cursor.side_effect = RuntimeError(SENSITIVE_ERROR)
        monkeypatch.setattr(health, "connection", database)
    if failed in {"cache", "both"}:
        backend = MagicMock()
        backend.set.side_effect = RuntimeError(SENSITIVE_ERROR)
        monkeypatch.setattr(health, "cache", backend)
    response = client.get(reverse("core:health_check"), HTTP_X_CORRELATION_ID="attacker-controlled")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["database"] == ("error" if failed in {"database", "both"} else "ok")
    assert data["cache"] == ("error" if failed in {"cache", "both"} else "ok")
    assert SENSITIVE_ERROR not in response.content.decode()
    assert "private-db" not in str(dict(response.headers))
    assert data["correlation_id"] != "attacker-controlled"
    # Newer pytest/Django logging can route a record through both the attached
    # handler and the root capture handler; count emitted records, not routes.
    records = list({id(record): record for record in caplog.records if record.name == "apps.core.health"}.values())
    assert len(records) == (2 if failed == "both" else 1)
    assert all(data["correlation_id"] in record.getMessage() for record in records)
    assert all(record.exc_info and SENSITIVE_ERROR in str(record.exc_info[1]) for record in records)


def test_silent_cache_write_failure_is_degraded(client, monkeypatch):
    backend = MagicMock()
    backend.get.return_value = None
    monkeypatch.setattr(health, "cache", backend)
    response = client.get(reverse("core:health_check"))
    assert response.status_code == 503
    assert response.json()["cache"] == "error"


def test_database_response_is_actually_verified(client, monkeypatch):
    database = MagicMock()
    database.cursor.return_value.__enter__.return_value.fetchone.return_value = (0,)
    monkeypatch.setattr(health, "connection", database)
    response = client.get(reverse("core:health_check"))
    assert response.status_code == 503
    assert response.json()["database"] == "error"


def test_probe_keys_are_unique_and_short_lived(client, monkeypatch):
    backend = MagicMock()
    backend.get.side_effect = lambda key: key.split(":", 1)[1]
    monkeypatch.setattr(health, "cache", backend)
    first = client.get(reverse("core:health_check")).json()
    second = client.get(reverse("core:health_check")).json()
    assert first["correlation_id"] != second["correlation_id"]
    for call in backend.set.call_args_list:
        assert call.kwargs["timeout"] == 5
        assert call.args[0] == f"health-probe:{call.args[1]}"
    assert backend.delete.call_count == 2


def test_health_allows_head_and_rejects_mutating_methods(client, monkeypatch):
    probe = MagicMock(return_value={"status": "ok", "database": "ok", "cache": "ok", "correlation_id": "test"})
    monkeypatch.setattr("apps.core.views.check_application_health", probe)
    response = client.head(reverse("core:health_check"))
    assert response.status_code == 200
    assert response.content == b""
    probe.reset_mock()
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(reverse("core:health_check")).status_code == 405
    probe.assert_not_called()
