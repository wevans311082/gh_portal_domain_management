# gh_portal_domain_management
a django based whmcs replacement with whm/cpanel and reseller club intergration for Grumpy hosting

## Container startup migrations

Migrations can run automatically on every container start via the entrypoint scripts.

- Set `AUTO_MAKEMIGRATIONS=1` to generate missing migration files at startup.
- `migrate --noinput` is always run at startup.
- Set `RUN_COLLECTSTATIC=1` for web containers, and `0` for celery/beat.

Development `docker-compose.yml` is configured with:

- `web`: `AUTO_MAKEMIGRATIONS=1`, `RUN_COLLECTSTATIC=1`
- `celery`: `AUTO_MAKEMIGRATIONS=1`, `RUN_COLLECTSTATIC=0`
- `celery-beat`: `AUTO_MAKEMIGRATIONS=1`, `RUN_COLLECTSTATIC=0`
