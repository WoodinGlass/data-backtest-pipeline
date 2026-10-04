# syntax=docker/dockerfile:1.6
# data-backtest-pipeline container.
# See docs/adr/0017-docker.md for the design rationale.

FROM python:3.11-slim

# --- Metadata ---
LABEL org.opencontainers.image.title="data-backtest-pipeline"
LABEL org.opencontainers.image.description="Reproducible daily US equity pipeline: ingestion -> dbt -> features -> risk -> backtest -> tracking -> orchestration."
LABEL org.opencontainers.image.licenses="MIT"

# --- Environment ---
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    DBP_DOCKER_BOOTSTRAP=1

# --- System dependencies ---
# git        : dbt packages resolve via git for hub installs
# tini       : PID 1 signal forwarding (avoids zombie processes)
# ca-certs   : HTTPS for yfinance/FRED/SEC/PyPI
# (no build-essential; all Python deps ship manylinux wheels)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Cache-friendly dependency install ---
# 1. Copy only metadata files.
# 2. Stub every project package with an empty __init__.py so
#    setuptools' package discovery does not fail.
# 3. Install the package in editable mode with all runtime extras.
#    The dependencies land in a layer that survives source edits.
# 4. Copy the real source on top; editable mode picks it up live.
#
# When the dependency list in pyproject.toml changes, this layer
# is invalidated and pip re-resolves. When only Python source
# changes, this layer is reused and a rebuild takes ~5 seconds.
COPY pyproject.toml README.md ./
RUN set -eux \
    && for pkg in ingestion features backtest tracking orchestration \
                   models monitoring app quality; do \
          mkdir -p "$pkg" && touch "$pkg/__init__.py"; \
       done \
    && pip install --no-cache-dir -e ".[dbt,quality,backtest,tracking,orchestration]"

# --- Real source (overwrites stubs) ---
COPY . .

# --- Non-root user ---
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /bin/bash app \
    && mkdir -p /app/data /app/mlruns /app/reports \
    && chown -R app:app /app

# --- Entrypoint ---
COPY --chown=app:app scripts/docker_entrypoint.sh /usr/local/bin/docker_entrypoint.sh
RUN chmod +x /usr/local/bin/docker_entrypoint.sh

USER app

# tini is PID 1; entrypoint then execs the user command.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker_entrypoint.sh"]

# Default: idle so the container stays up for `docker compose exec`.
# `make pipeline` overrides this with a one-shot command.
CMD ["sleep", "infinity"]
