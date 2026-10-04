.PHONY: help install install-all lint format test test-int test-all dbt-build ingest ingest-macro ingest-sec quality quality-json features features-info backtest app orchestrate clean ci ci-docker ci-full up down logs shell run pipeline docker-build prefect-up prefect-down


help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install package with dev extras
	pip install -e ".[dev,dbt,quality,backtest,tracking,orchestration,app]"

install-all:  ## Install package with all extras (recommended for Colab/dev)
	pip install -e ".[dev,dbt,quality,backtest,tracking,orchestration,app,integration,observability,snowflake]"

lint:  ## Run ruff and mypy
	ruff check .
	ruff format --check .
	mypy ingestion

format:  ## Auto-format code
	ruff check --fix .
	ruff format .

test:  ## Run unit tests
	pytest -m "not integration and not slow"

test-int:  ## Run integration tests (needs Docker/network)
	pytest -m "integration"

quality:  ## Run the data quality gate (raw + staging + marts)
	python -m quality.cli

quality-json:  ## Run the gate and write a JSON report to ./reports/quality.json
	python -m quality.cli --json reports/quality.json

ci:  ## Mirror the GitHub CI suite locally (lint + type + test + coverage + dbt + quality)
	ruff check .
	ruff format --check .
	mypy ingestion quality
	pytest -m "not integration and not slow" \
		--cov --cov-report=term-missing --cov-fail-under=70
	$(MAKE) dbt-build FIXTURE=1
	RAW_DATA_DIR=tests/fixtures/raw python -m quality.cli --tickers-from-raw

ci-docker:  ## Mirror the GitHub docker-build job locally (requires Docker)
	docker build -t dbp-pipeline:ci .
	docker run --rm dbp-pipeline:ci python -c "import ingestion, backtest, tracking, orchestration; print('imports ok')"
	docker run --rm dbp-pipeline:ci python -m orchestration.cli list

ci-full: ci ci-docker  ## Run every CI job locally, including docker-build


test-all:  ## Run all tests with coverage
	pytest --cov --cov-report=term-missing

ingest-macro:  ## Fetch macro series into the raw layer
	python -m ingestion.macro.cli

ingest-sec:  ## Fetch SEC EDGAR fundamentals into the raw layer
	python -m ingestion.sec.cli

ingest:  ## Fetch daily bars into the raw layer (START=YYYY-MM-DD END=YYYY-MM-DD)
	python -m ingestion.cli $(if $(START),--start $(START)) $(if $(END),--end $(END))

dbt-build:  ## Run dbt build (use FIXTURE=1 for the committed CI fixture)
	@if [ "$(FIXTURE)" = "1" ]; then \
		echo "dbt build against fixture (tests/fixtures/...)"; \
		dbt build --project-dir dbt --profiles-dir dbt \
			--vars '{"raw_prices_glob": "tests/fixtures/raw/prices/yfinance/*/*.parquet"}'; \
	else \
		echo "dbt build against real raw layer (data/raw/...)"; \
		dbt build --project-dir dbt --profiles-dir dbt; \
	fi

features:  ## Build the point-in-time feature table
	python -m features.cli

features-info:  ## Print feature file info
	python -m features.cli --info

backtest:  ## Run the walk-forward backtest
	python scripts/run_backtest.py

app:  ## Start the Streamlit dashboard
	streamlit run app/streamlit_app.py

# --- Docker ---------------------------------------------------------------

docker-build:  ## Build the pipeline image
	docker compose build

up:  ## Build + start the worker container (background)
	docker compose up -d --build

down:  ## Stop and remove containers (state on ./data persists)
	docker compose down

logs:  ## Tail worker logs
	docker compose logs -f worker

shell:  ## Interactive bash inside the worker container
	docker compose exec worker bash

run:  ## Run one command in the worker (usage: make run CMD='...')
	@if [ -z "$(CMD)" ]; then \
		echo "usage: make run CMD='<shell command>'"; \
		exit 2; \
	fi
	docker compose exec worker sh -lc "$(CMD)"

pipeline:  ## Run the full pipeline once in a throwaway container
	docker compose run --rm worker sh -lc "\
		python -m ingestion.cli && \
		python -m ingestion.macro.cli && \
		python -m ingestion.sec.cli && \
		dbt build --project-dir dbt --profiles-dir dbt && \
		python -m quality.cli --tickers-from-raw --json reports/quality.json && \
		python -m features.cli && \
		python scripts/run_backtest.py --mlflow"

prefect-up:  ## Start worker + Prefect server (profile: served)
	docker compose --profile served up -d --build

prefect-down:  ## Stop worker + Prefect server
	docker compose --profile served down

orchestrate:  ## Run a flow inside the worker (usage: make orchestrate FLOW=daily_refresh)
	@if [ -z "$(FLOW)" ]; then \
		echo "usage: make orchestrate FLOW=<flow-name> [ARG=key=value ...]"; \
		exit 2; \
	fi
	docker compose exec worker python -m orchestration.cli run $(FLOW) $(if $(ARG),--arg $(ARG),)

# --- End Docker -----------------------------------------------------------

clean:  ## Remove build artifacts and caches
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +