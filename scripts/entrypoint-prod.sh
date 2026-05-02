#!/bin/bash
set -e

echo "Waiting for database..."
python manage.py wait_for_db

if [ "${RUN_STARTUP_MIGRATIONS:-1}" = "1" ]; then
    echo "Running startup migrations (RUN_STARTUP_MIGRATIONS=1)..."

    python manage.py shell <<'PY'
import os
from django.core.management import call_command
from django.db import connection

lock_id = int(os.environ.get("MIGRATION_LOCK_ID", "8453201"))
is_leader = os.environ.get("MIGRATION_LEADER", "0") == "1"
auto_makemigrations = os.environ.get("AUTO_MAKEMIGRATIONS", "0") == "1"

print(f"Acquiring migration advisory lock {lock_id}...")
with connection.cursor() as cursor:
    cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])

try:
    if is_leader and auto_makemigrations:
        print("Leader node: resolving merge conflicts (if any)...")
        call_command("makemigrations", merge=True, interactive=False, verbosity=1)
        print("Leader node: auto-generating migrations...")
        call_command("makemigrations", interactive=False, verbosity=1)
    elif auto_makemigrations:
        print("AUTO_MAKEMIGRATIONS=1 but this node is not leader; skipping makemigrations.")

    print("Applying migrations...")
    call_command("migrate", interactive=False, verbosity=1)
finally:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
    print("Released migration advisory lock.")
PY
else
    echo "Skipping startup migrations (RUN_STARTUP_MIGRATIONS=0)."
fi

if [ "${RUN_COLLECTSTATIC:-1}" = "1" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
fi

echo "Starting gunicorn..."
exec gunicorn grumpy_portal.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 4 \
    --worker-class gthread \
    --threads 2 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
