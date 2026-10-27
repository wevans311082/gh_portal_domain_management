"""Unicode lowercasing for SQLite search indexes, on every Django connection."""
from django.db import connections
from django.db.backends.signals import connection_created


def register_search_functions(sender=None, connection=None, **kwargs):
    if connection is not None and connection.vendor == 'sqlite' and connection.connection is not None:
        connection.connection.create_function('PORTAL_LOWER', 1, lambda value: value.lower() if value is not None else None,
                                              deterministic=True)


def install():
    connection_created.connect(register_search_functions, dispatch_uid='admin-search-sqlite-functions')
    for connection in connections.all(initialized_only=True):
        register_search_functions(connection=connection)
