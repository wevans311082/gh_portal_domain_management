"""Bounded health probes with public states and private correlated diagnostics."""
import logging
import uuid

from django.core.cache import cache
from django.db import connection

logger = logging.getLogger(__name__)


def check_application_health():
    correlation_id = uuid.uuid4().hex
    state = {"status": "ok", "database": "ok", "cache": "ok", "correlation_id": correlation_id}

    try:
        # Opening a connection alone does not test an existing stale connection.
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise RuntimeError("Database health probe returned an unexpected result")
    except Exception:
        state["database"] = "error"
        logger.exception("Health probe failed component=database correlation_id=%s", correlation_id)

    # A unique key/value prevents concurrent probes or stale cached values from
    # concealing failed writes. A short TTL bounds residue after process failure.
    cache_key = f"health-probe:{correlation_id}"
    try:
        cache.set(cache_key, correlation_id, timeout=5)
        if cache.get(cache_key) != correlation_id:
            raise RuntimeError("Cache health probe did not read its own write")
        cache.delete(cache_key)
    except Exception:
        state["cache"] = "error"
        logger.exception("Health probe failed component=cache correlation_id=%s", correlation_id)

    if state["database"] != "ok" or state["cache"] != "ok":
        state["status"] = "degraded"
    return state
