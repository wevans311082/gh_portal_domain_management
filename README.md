# gh_portal_domain_management
a django based whmcs replacement with whm/cpanel and reseller club intergration for Grumpy hosting

## Container startup migrations

Migrations can run automatically on every container start via the entrypoint scripts.

- Set `RUN_STARTUP_MIGRATIONS=1` for exactly one leader container (usually `web`).
- Set `MIGRATION_LEADER=1` on the leader and `0` on other services.
- Set `AUTO_MAKEMIGRATIONS=1` only on the leader if you want model changes auto-generated.
- Startup migration flow uses a Postgres advisory lock (`MIGRATION_LOCK_ID`) to prevent concurrent migration races.
- Leader also runs `makemigrations --merge` before `makemigrations` to auto-resolve leaf-node merge conflicts when possible.
- Set `RUN_COLLECTSTATIC=1` for web containers, and `0` for celery/beat.

Development `docker-compose.yml` is configured with:

- `web`: `RUN_STARTUP_MIGRATIONS=1`, `MIGRATION_LEADER=1`, `AUTO_MAKEMIGRATIONS=1`, `RUN_COLLECTSTATIC=1`
- `celery`: `RUN_STARTUP_MIGRATIONS=0`, `MIGRATION_LEADER=0`, `AUTO_MAKEMIGRATIONS=0`, `RUN_COLLECTSTATIC=0`
- `celery-beat`: `RUN_STARTUP_MIGRATIONS=0`, `MIGRATION_LEADER=0`, `AUTO_MAKEMIGRATIONS=0`, `RUN_COLLECTSTATIC=0`
