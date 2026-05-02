#!/bin/bash
set -e

echo "Waiting for database..."
python manage.py wait_for_db

if [ "${AUTO_MAKEMIGRATIONS:-0}" = "1" ]; then
    echo "Auto-generating migrations (AUTO_MAKEMIGRATIONS=1)..."
    python manage.py makemigrations --noinput
fi

echo "Running migrations..."
python manage.py migrate --noinput

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
