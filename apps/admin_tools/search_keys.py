"""Collation-stable expression shared by search predicates and indexes."""
from django.db.models import Func, CharField
from django.db.models.functions import Lower


class FullName(Func):
    output_field = CharField()

    def as_sql(self, compiler, connection, **extra_context):
        first, first_params = compiler.compile(self.source_expressions[0])
        last, last_params = compiler.compile(self.source_expressions[1])
        # Fixed literals let the database recognize the same expression in an
        # index and a query; no request text is interpolated into SQL.
        return f"(COALESCE({first}, '') || ' ' || COALESCE({last}, ''))", [*first_params, *last_params]


class SearchKey(Lower):
    def as_sqlite(self, compiler, connection, **extra_context):
        sql, params = super().as_sql(compiler, connection, function='PORTAL_LOWER', **extra_context)
        return f'({sql} COLLATE BINARY)', params

    def as_postgresql(self, compiler, connection, **extra_context):
        sql, params = super().as_sql(compiler, connection, **extra_context)
        return f'({sql} COLLATE "C")', params
