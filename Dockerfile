# ─────────────────────────────────────────────────────────────────
# SEC RAG Pipeline — application image
# Base: python:3.11-slim (matches pyproject.toml python = "^3.11")
# Poetry: 2.1.1 (matches poetry.lock format version)
# ─────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# System deps needed by lxml, psycopg2, and curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry — pin to 2.1.1 to match regenerated poetry.lock
RUN pip install --no-cache-dir poetry==2.1.1

# Disable virtualenv creation inside container (we run as root in /app)
ENV POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

# Copy dependency files first (layer caching — only re-installs
# when pyproject.toml or poetry.lock changes, not on code changes)
COPY pyproject.toml poetry.lock ./

# Install all dependencies including dev (for ruff/mypy/pytest in CI)
# --no-root: don’t install the project package itself yet
RUN poetry install --no-interaction --no-root

# Copy the rest of the source
# (overridden by volume mount in docker compose dev mode)
COPY . .

# Default: keep container alive for exec usage
# Override via docker compose command: field
CMD ["tail", "-f", "/dev/null"]
