# syntax=docker/dockerfile:1.7
#
# Fast multi-stage build:
# - No build-essential/gcc: all Python deps ship manylinux wheels (psycopg2-binary, Pillow, cryptography).
# - BuildKit pip cache across rebuilds.
# - Optional wkhtmltopdf (invoice PDFs); disable with --build-arg INSTALL_WKHTMLTOPDF=0 for even faster builds.
# - Shared image for web/celery/beat via compose image name.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---------------------------------------------------------------------------
# Builder: install Python packages into an isolated prefix
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60

WORKDIR /build

# Only requirements first → maximal layer cache hit when app code changes
COPY requirements/ requirements/

# development.txt by default (includes debug toolbar / pytest for the lab stack)
ARG REQUIREMENTS_FILE=requirements/development.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r "${REQUIREMENTS_FILE}"

# ---------------------------------------------------------------------------
# Runtime: slim image + optional PDF engine
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ARG INSTALL_WKHTMLTOPDF=1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime OS packages only — never compile toolchains here.
# libpq not required when using psycopg2-binary (bundles its own libpq).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    ; \
    if [ "${INSTALL_WKHTMLTOPDF}" = "1" ]; then \
        apt-get install -y --no-install-recommends \
            fontconfig \
            fonts-liberation \
            fonts-dejavu-core \
            libjpeg62-turbo \
            libxrender1 \
            libxext6 \
            libx11-6 \
            xfonts-75dpi \
            xfonts-base \
            wget \
        ; \
        ARCH="$(dpkg --print-architecture)"; \
        wget -q -O /tmp/wkhtmltox.deb \
          "https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_${ARCH}.deb"; \
        dpkg -i /tmp/wkhtmltox.deb || apt-get install -y -f --no-install-recommends; \
        rm -f /tmp/wkhtmltox.deb; \
        apt-get purge -y wget; \
        apt-get autoremove -y; \
        wkhtmltopdf --version || true; \
    fi; \
    rm -rf /var/lib/apt/lists/*

# Python site-packages from builder
COPY --from=builder /install /usr/local

# Application source (context is filtered by .dockerignore)
COPY manage.py pytest.ini ./
COPY cyberask_domains/ cyberask_domains/
COPY apps/ apps/
COPY templates/ templates/
COPY static/ static/
COPY scripts/ scripts/
COPY nginx/ nginx/

RUN chmod +x scripts/entrypoint.sh scripts/entrypoint-prod.sh \
    && useradd --create-home --shell /bin/bash --uid 10001 appuser \
    && chown -R appuser:appuser /app

# Keep root for entrypoint migrations in lab; prod compose can drop privileges later.
EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
