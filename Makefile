.PHONY: help install lint format test test-int test-all dbt-build ingest quality quality-json backtest app up down clean ci ingest-macro install-all ingest-sec features features-info


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

ci:  ## Run the full CI suite locally (lint + test + dbt + quality)
	ruff check .
	ruff format --check .
	mypy ingestion quality
	pytest -m "not integration and not slow"
	$(MAKE) dbt-build FIXTURE=1
	RAW_DATA_DIR=tests/fixtures/raw python -m quality.cli --tickers-from-raw

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

up:  ## Start all services (docker compose)
	docker compose up -d --build

down:  ## Stop all services
	docker compose down

clean:  ## Remove build artifacts and caches
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +