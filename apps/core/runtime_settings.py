"""Runtime setting accessors with DB-first fallback."""
from django.conf import settings
from django.apps import apps
from django.db import OperationalError, ProgrammingError

_SENTINEL = object()


def _from_db(key: str):
    try:
        from apps.admin_tools.models import IntegrationSetting

        item = IntegrationSetting.objects.filter(key=key).first()
        return item.plaintext_value if item is not None else _SENTINEL
    except (OperationalError, ProgrammingError):
        # Import-time callers must allow initial database creation. Once the
        # application is ready, storage failures must not switch credentials.
        if not apps.ready:
            return _SENTINEL
        raise


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
    value, _source = get_runtime_setting_with_source(key, default)
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
