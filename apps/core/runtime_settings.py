"""Runtime setting accessors with DB-first fallback."""
from django.conf import settings
from django.core.cache import cache

_CACHE_TTL = 60
_SENTINEL = object()


def _from_db(key: str):
    try:
        from apps.admin_tools.models import IntegrationSetting

        value = IntegrationSetting.objects.filter(key=key).values_list("value", flat=True).first()
        if value in (None, ""):
            return _SENTINEL
        return value
    except Exception:
        return _SENTINEL


def get_runtime_setting_with_source(key: str, default=""):
    """Return ``(value, source)`` where source is database, environment, or default."""
    db_value = _from_db(key)
    if db_value is not _SENTINEL:
        return db_value, "database"

    if hasattr(settings, key):
        value = getattr(settings, key)
        if value not in (None, ""):
            return value, "environment"
        if default not in (None, ""):
            return default, "default"
        return value, "environment"

    return default, "default"


def get_runtime_setting(key: str, default=""):
    cache_key = f"runtime_setting:{key}"
    cached = cache.get(cache_key, _SENTINEL)
    if cached is not _SENTINEL:
        return cached

    value, _source = get_runtime_setting_with_source(key, default)
    cache.set(cache_key, value, timeout=_CACHE_TTL)
    return value


def get_runtime_int(key: str, default: int = 0) -> int:
    value = get_runtime_setting(key, default)
    try:
        return int(value)
    except Exception:
        return default


def get_runtime_bool(key: str, default: bool = False) -> bool:
    value = str(get_runtime_setting(key, default)).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return bool(default)


def get_runtime_list(key: str, default=None):
    if default is None:
        default = []
    value = get_runtime_setting(key, default)
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(default)
