# ─────────────────────────────────────────────────────────────────
# SEC RAG Pipeline — application image
# Base: python:3.11-slim (matches pyproject.toml python = "^3.11")
# ─────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# System deps needed by lxml, psycopg2, and curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry — pin to same version as CI (1.8.3)
RUN pip install --no-cache-dir poetry==1.8.3

WORKDIR /app

# Copy dependency files first (layer caching — only re-installs
# when pyproject.toml or poetry.lock changes, not on code changes)
COPY pyproject.toml poetry.lock ./

# Install all dependencies including dev (for ruff/mypy/pytest)
# --no-root: don't install the project package itself yet (mounted as volume)
RUN poetry install --no-interaction --no-root

# Copy the rest of the source (will be overridden by volume mount in dev)
COPY . .

# Default command: keep container alive for exec usage
# Override with `docker compose run app poetry run python ...`
CMD ["tail", "-f", "/dev/null"]
