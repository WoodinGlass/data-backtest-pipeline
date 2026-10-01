FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
RUN pip install --upgrade pip

# Copy source so `pip install -e .` can find packages
COPY ingestion/       ./ingestion/
COPY features/        ./features/
COPY backtest/        ./backtest/
COPY models/          ./models/
COPY orchestration/   ./orchestration/
COPY monitoring/      ./monitoring/
COPY app/             ./app/

RUN pip install -e ".[dbt,quality,tracking,orchestration,app,observability]"

COPY . .

# Default: keep container alive so `docker compose exec` works.
CMD ["sleep", "infinity"]
