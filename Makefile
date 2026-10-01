.PHONY: help install lint format test test-int test-all \
        dbt-build ingest backtest app up down clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install package with dev extras
	pip install -e ".[dev,dbt,quality,tracking,orchestration,app]"

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

test-all:  ## Run all tests with coverage
	pytest --cov --cov-report=term-missing

ingest:  ## Fetch daily bars into the raw layer (START=YYYY-MM-DD END=YYYY-MM-DD)
	python -m ingestion.cli $(if $(START),--start $(START)) $(if $(END),--end $(END))

dbt-build:  ## Run dbt models and tests
	dbt build --project-dir dbt --profiles-dir dbt

backtest:  ## Run the walk-forward backtest
	python -m backtest.run

app:  ## Start the Streamlit dashboard
	streamlit run app/streamlit_app.py

up:  ## Start all services (docker compose)
	docker compose up -d --build

down:  ## Stop all services
	docker compose down

clean:  ## Remove build artifacts and caches
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
