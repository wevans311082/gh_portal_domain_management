# Cyber Ask Domains

Django client portal for domain registration and management with ResellerClub and WHM/cPanel integration.

Product brand: **Cyber Ask Domains** (Cyber Ask Ltd).  
Internal Python package: `cyberask_domains`.

## Core features

- Domain lookup and registration (ResellerClub)
- Domain management (renew, lock, nameservers, contacts, EPP)
- Cart → invoice checkout
- Hosting package shop + cPanel SSO pass-through (WHM `create_user_session`)

## Quick start (Docker)

```bash
cp .env.example .env
# edit secrets: DJANGO_SECRET_KEY, ResellerClub, WHM, etc.
docker compose up --build -d
```

- App (direct): http://localhost:8000  
- Nginx: http://localhost  
- Health: http://localhost:8000/health/

### Why builds are fast

The Docker setup is tuned for short rebuilds:

| Technique | Effect |
|-----------|--------|
| `.dockerignore` | Drops `website_templates/extracted` (~hundreds of MB), `.venv`, tests, caches from the build context |
| Multi-stage `Dockerfile` | Installs wheels only — **no `build-essential`/gcc** (psycopg2-binary, Pillow, cryptography all ship wheels) |
| Pin `python:3.12-slim-bookworm` | Matches the wkhtmltopdf bookworm `.deb` (avoids slow `apt -f` repair on Trixie) |
| BuildKit pip cache | `RUN --mount=type=cache,target=/root/.cache/pip` reuses downloads across builds |
| Shared image | `web` / `celery` / `celery-beat` all use `cyberask-domains:local` — **one** build, three services |
| Optional PDF engine | `docker compose build --build-arg INSTALL_WKHTMLTOPDF=0` skips wkhtmltopdf for the fastest lab image |

```bash
# normal rebuild (one shared image)
docker compose build

# fastest lab image (no invoice PDF binary)
docker compose build --build-arg INSTALL_WKHTMLTOPDF=0

# production image
docker build -f Dockerfile.prod -t cyberask-domains:prod .
```

## Container startup migrations

Migrations can run automatically on every container start via the entrypoint scripts.

- Set `RUN_STARTUP_MIGRATIONS=1` for exactly one leader container (usually `web`).
- Set `MIGRATION_LEADER=1` on the leader and `0` on other services.
- Set `AUTO_MAKEMIGRATIONS=1` only on the leader if you want model changes auto-generated.
- Startup migration flow uses a Postgres advisory lock (`MIGRATION_LOCK_ID`) to prevent concurrent migration races.
- Set `RUN_COLLECTSTATIC=1` for web containers, and `0` for celery/beat.

## Settings module

```text
DJANGO_SETTINGS_MODULE=cyberask_domains.settings.development
```

Celery:

```text
celery -A cyberask_domains worker -l info
```

## Note on database identity

Docker Compose still creates Postgres credentials `grumpy` / `grumpy_portal` by default so existing deployment `.env` `DATABASE_URL` values keep working. Do not commit real `.env` files.
