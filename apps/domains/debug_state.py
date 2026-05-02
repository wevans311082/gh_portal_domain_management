"""Thread-local request debug capture for ResellerClub HTTP calls."""

from threading import local

_state = local()
_MAX_ENTRIES = 25


def reset_entries():
    _state.entries = []


def add_entry(entry: dict):
    entries = getattr(_state, "entries", [])
    entries.append(entry)
    if len(entries) > _MAX_ENTRIES:
        entries = entries[-_MAX_ENTRIES:]
    _state.entries = entries


def get_entries():
    return list(getattr(_state, "entries", []))
