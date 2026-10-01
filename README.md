# data-backtest-pipeline

> A production-style data and ML pipeline: ingestion → warehouse → features → backtest → monitoring.

![CI](https://github.com/WoodinGlass/data-backtest-pipeline/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Table of Contents

1. [What this is / is not](#what-this-is--is-not)
2. [Problem](#problem)
3. [Key Design Decisions](#key-design-decisions)
4. [Architecture](#architecture)
5. [Tech Stack](#tech-stack)
6. [Data Contracts](#data-contracts)
7. [Pipeline Layers](#pipeline-layers)
8. [Idempotency and Failure Modes](#idempotency-and-failure-modes)
9. [Universe & Data Source](#universe--data-source)
10. [Quickstart](#quickstart)
11. [Configuration](#configuration)
12. [Project Structure](#project-structure)
13. [Testing Strategy](#testing-strategy)
14. [CI/CD](#cicd)
15. [Observability and Monitoring](#observability-and-monitoring)
16. [Results](#results)
17. [Limitations](#limitations)
18. [Documentation](#documentation)
19. [Contributing](#contributing)
20. [Roadmap](#roadmap)
21. [License](#license)

---

## What this is / is not

**This is:**
- A portfolio project that demonstrates end-to-end data engineering and ML evaluation practices on **US equity daily data**.
- A reproducible pipeline: public market data → immutable raw layer → tested dbt models → point-in-time features → walk-forward backtest → monitored dashboard.
- An example of how to evaluate probabilistic models honestly (log loss, Brier score, calibration, Sharpe, max drawdown) **without data leakage or survivorship bias**.
- A demonstration that a small, well-built model **often fails to beat buy-and-hold** — and that reporting that honestly is the correct engineering outcome.

**This is not:**
- A trading bot. It does not place orders through any broker.
- Financial advice. Backtest results do not guarantee future performance.
- A real-time or intraday system. Ingestion is **daily batch** by design.
- A high-frequency or alpha-generating strategy. Signals in daily equity data are weak and noisy by nature; the point is the pipeline, not the alpha.
- A general-purpose ML platform. Scope is limited to daily US equities and one pipeline.

---

## Problem

Backtests are often unreliable because of hidden data leakage, **survivorship bias**, non-reproducible data, and uncalibrated probabilities. This project builds a pipeline where every number in a backtest can be traced back to immutable raw data, tested transformations, and a versioned model.

> One-sentence problem statement: **How can we build a reproducible, leakage-free pipeline that turns daily US equity prices into honestly evaluated probabilistic direction predictions, and monitors them over time?**

**Target users:** data engineers, ML engineers, and reviewers who want to see production-style practices applied to financial time-series in a compact project.

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Domain | **US equities, S&P 500 subset (~30 tickers), daily bars** | Clean data, standard benchmarks, honest evaluation |
| Payload vs pipeline | **Pipeline is the product**; the model is the payload | Focus is on reliability, testing, and reproducibility |
| Deterministic parts | Ingestion (given cached raw), dbt models, feature builders, backtest | Same input → same output; hash of input is the idempotency key |
| Non-deterministic parts | Model training (seeded), upstream price revisions | Seeds are fixed; raw is stored immutably |
| Storage tier | DuckDB (local warehouse) + Parquet (raw/artifacts); Snowflake optional | Small structured data, analytical queries |
| Ingestion mode | **Batch, daily** | Simple, cheap, sufficient for daily bars |
| Schema failures | **Hard fail** | Bad schema means corrupt data |
| Freshness/volume issues | **Warn first**, then fail after threshold | Recoverable conditions |
| Config | Environment variables + typed settings | No hardcoded paths or backends |
| Prediction target | `P(return_{t+1} > 0)` per ticker-day | Binary, calibratable, comparable across tickers |
| Primary benchmark | **Buy-and-hold SPY** | The honest baseline nobody beats consistently |

Full rationale is recorded in `docs/adr/`.

---

## Architecture

```text
            ┌────────────┐
 yfinance   │ ingestion/ │  retry, backoff, immutable Parquet
            └─────┬──────┘
                  ▼
            ┌────────────┐
            │  raw layer │  immutable, append-only (ticker, trade_date)
            └─────┬──────┘
                  ▼
            ┌────────────┐
            │    dbt     │  staging → intermediate → marts
            └─────┬──────┘
                  ▼
            ┌────────────┐
            │ DQ gate    │  Pandera / Great Expectations
            └─────┬──────┘
                  ▼
            ┌────────────┐
            │ features/  │  point-in-time builders + anti-leakage tests
            └─────┬──────┘
                  ▼
            ┌────────────┐     ┌────────┐
            │ backtest/  │────▶│ MLflow │
            └─────┬──────┘     └────────┘
                  ▼
            ┌────────────┐
            │ monitoring │  drift, freshness, model performance
            └─────┬──────┘
                  ▼
            ┌────────────┐
            │ Streamlit  │  dashboard
            └────────────┘

 Orchestration: Prefect (scheduled flows + failure alerts)
```

---

## Tech Stack

- **Language:** Python 3.11
- **Market data:** `yfinance` (default, no API key), optional `Stooq` fallback
- **Warehouse:** DuckDB (default), Snowflake (optional)
- **Transformation:** dbt
- **Data quality:** Pandera / Great Expectations, dbt tests
- **Orchestration:** Prefect
- **Experiment tracking:** MLflow
- **Containers:** Docker, docker-compose
- **CI/CD:** GitHub Actions
- **Dashboard:** Streamlit
- **Tooling:** ruff, mypy, pytest, pre-commit, Makefile

---

## Data Contracts

Schemas are defined before any producer or consumer code is written.

```python
from datetime import date, datetime
from decimal import Decimal
from pydantic import BaseModel, Field


class RawPriceEvent(BaseModel):
    \"\"\"One immutable daily OHLCV bar for a single ticker.\"\"\"

    source: str
    ticker: str
    trade_date: date
    ingested_at: datetime
    payload_hash: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_close: Decimal
    volume: int = Field(ge=0)

    @property
    def idempotency_key(self) -> str:
        return f"{self.source}:{self.ticker}:{self.trade_date.isoformat()}:{self.payload_hash}"
```

Contracts live in `ingestion/schemas.py` and are documented in `docs/data_dictionary.md`.

---

## Pipeline Layers

### 1. Ingestion (`ingestion/`)
- Batch pull from `yfinance` with retries and exponential backoff with jitter.
- Idempotent writes: `(source, ticker, trade_date, payload_hash)` as key.
- Raw layer is immutable and stored as daily Parquet partitions. Corrections (splits, dividends, restatements) are **new rows**, never updates.

### 2. Warehouse (`dbt/`)
- `staging`: typed, renamed OHLCV 1:1 with raw.
- `intermediate`: adjusted-return computation, corporate-action handling, universe filtering.
- `marts`: `dim_tickers`, `fct_prices_daily`, `fct_returns_daily`, `fct_predictions`.
- Tests: `not_null`, `unique`, `relationships`, and source freshness.

### 3. Data quality gate
- Pandera / Great Expectations checks run before features are built.
- Checks: no gaps > N trading days, `low ≤ open,close ≤ high`, non-negative volume, sane return bounds.
- The pipeline **fails** if the data is bad. No silent passes.

### 4. Features (`features/`)
- Point-in-time feature builders: a feature for date `t` only uses data available strictly before `t`.
- Features: lagged returns, rolling volatility, RSI, momentum, volume ratios, cross-sectional ranks.
- Anti-leakage tests: shuffle future rows, assert features for date `t` do not change.

### 5. Backtest (`backtest/`)
- Walk-forward evaluation with expanding or rolling windows.
- Baselines: 50/50 naive, momentum, buy-and-hold SPY.
- Main model: calibrated classifier (logistic regression or gradient boosting + Platt/isotonic).
- Metrics: log loss, Brier, calibration, hit rate, Sharpe, max drawdown, ROI vs SPY.

### 6. Models (`models/`)
- Train, calibrate, and register models. Every run tracked in MLflow.

### 7. Orchestration (`orchestration/`)
- Prefect flows on a daily schedule after US market close.

### 8. Monitoring (`monitoring/`)
- Data freshness, feature drift, prediction drift, rolling model performance vs buy-and-hold.

### 9. Dashboard (`app/`)
- Streamlit: pipeline health, backtest results, calibration, equity curve vs SPY, drift.

---

## Idempotency and Failure Modes

**Idempotency:** running any write step twice must produce the same final state.

| Operation | Key | Strategy |
|---|---|---|
| Insert raw prices | `(source, ticker, trade_date, payload_hash)` | `ON CONFLICT DO NOTHING` |
| Refetch after revision | New `payload_hash` | New immutable row |
| Feature build | `(feature_version, ticker, date)` | Overwrite the same partition |
| Model run | Run ID | Tracked in MLflow |

**Failure modes:**

| What can fail | Impact | Strategy |
|---|---|---|
| `yfinance` returns empty / partial | Missing bars | Retry with jitter, fall back to cached raw, freshness warning |
| `yfinance` rate-limits | Ingestion delayed | Backoff, batch tickers across time |
| Upstream price revision | Inconsistent series | Store as new row; recompute in dbt |
| Ticker delisted | **Survivorship bias** | Universe is defined as-of date, not as-of today |
| Warehouse | Pipeline stops | Retry, fail loudly, alert |
| Bad data | Wrong returns | DQ gate fails the run |
| Network | Intermittent errors | Retry with jitter |

**Deliberate non-strategy:** we do **not** silently forward-fill missing prices, and we do **not** drop delisted tickers.

---

## Universe & Data Source

**Universe:** ~30 large-cap, highly liquid S&P 500 tickers (configurable via `config/universe.txt`). Members are defined **as-of each backtest date**, not as-of today.

**Benchmark:** `SPY`.

**Data source:** `yfinance` — free, no API key. Raw responses cached to Parquet immediately so the pipeline is reproducible without network.

**Frequency:** daily bars, fetched after US market close.

---

## Quickstart

**Requirements:** Python 3.11, Docker, Make.

```bash
git clone https://github.com/WoodinGlass/data-backtest-pipeline.git
cd data-backtest-pipeline

cp .env.example .env
make up
```

Useful commands:

```bash
make install     # install dependencies
make lint        # ruff + mypy
make test        # unit tests
make test-int    # integration tests
make ingest      # fetch daily bars into the raw layer
make dbt-build   # run dbt models and tests
make backtest    # run the walk-forward backtest
make app         # start the Streamlit dashboard
make down        # stop services
```

---

## Configuration

All configuration is via environment variables. See `.env.example`.

```bash
WAREHOUSE_BACKEND=duckdb
DUCKDB_PATH=./data/warehouse.duckdb

PRICE_SOURCE=yfinance
UNIVERSE_FILE=./config/universe.txt
BENCHMARK_TICKER=SPY
PRICE_HISTORY_START=2015-01-01

MLFLOW_TRACKING_URI=./mlruns

LOG_LEVEL=INFO
LOG_FORMAT=json
```

Dependencies live in `pyproject.toml` with self-contained extras: `dev`, `dbt`, `snowflake`, `integration`, `observability`, `orchestration`, `tracking`, `quality`, `app`.

---

## Project Structure

```text
├── ingestion/        # yfinance client, retry, raw schema, Parquet writer
├── dbt/
│   ├── models/{staging,intermediate,marts}/
│   ├── tests/  snapshots/  macros/
├── features/         # point-in-time feature builders + anti-leakage tests
├── backtest/         # walk-forward, metrics, staking
├── models/           # train, calibrate, registry
├── orchestration/    # Prefect flows
├── monitoring/       # drift, data freshness
├── app/              # Streamlit dashboard
├── config/           # universe list, feature configs
├── tests/{unit,integration}/
├── docs/             # ADR, data dictionary, runbook
├── .github/workflows/ci.yml
├── Dockerfile  docker-compose.yml  Makefile
└── pyproject.toml  .pre-commit-config.yaml
```

---

## Testing Strategy

| Tier | Scope | Marker |
|---|---|---|
| Unit | Pure functions, no external services (default) | none |
| Integration | Needs DuckDB/Docker/network | `@pytest.mark.integration` |
| Slow | Long-running backtests | `@pytest.mark.slow` |

Key tests:
- **Anti-leakage:** features for date `t` never change when future rows are shuffled.
- **Survivorship:** universe membership is date-aware; delisted tickers remain.
- **Idempotency:** running ingestion twice does not duplicate rows.
- **dbt tests:** `not_null`, `unique`, `relationships`, source freshness.
- **Metrics:** log loss, Brier, Sharpe, max drawdown checked against hand-computed values.
- **Corporate actions:** a synthetic 2:1 split does not produce a spurious −50% return.

---

## CI/CD

GitHub Actions runs on every push and pull request:

1. Install dependencies
2. Lint (ruff) and type-check (mypy)
3. Unit tests (pytest)
4. `dbt build` on a small committed sample
5. Smoke test on a 5-ticker, 30-day fixture

Merges are blocked if any step fails. Commits follow [Conventional Commits](https://www.conventionalcommits.org/).

---

## Observability and Monitoring

- **Structured JSON logs** with correlation IDs.
- **Data freshness:** alert when latest `trade_date` in marts is stale.
- **Drift:** feature distribution shift vs training window.
- **Model performance:** rolling log loss, Brier, hit rate, Sharpe, cumulative return vs SPY.
- **Failure alerts:** sent from Prefect flows.

Severity policy: schema violation → hard fail; freshness/volume → warning first; integrity check → soft fail.

---

## Results

> To be filled in after M5 and M12. Claims about performance require numbers.

| Model | Log loss | Brier | Hit rate | Sharpe | Max DD | ROI vs SPY |
|---|---|---|---|---|---|---|
| Naive 50/50 | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| Momentum (last return) | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| Buy-and-hold SPY | — | — | — | [ ] | [ ] | 0.00 |
| Main model (calibrated) | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |

- Calibration curve: `docs/preview/calibration.png`
- Cumulative returns vs SPY: `docs/preview/equity_curve.png`

**Honest reporting policy:** if the main model does not beat buy-and-hold, this table will say so.

---

## Limitations

- Backtests rely on historical data and cannot capture regime changes.
- **Survivorship and look-ahead bias are enemies #1 and #2.** We address both, but no mitigation is perfect.
- `yfinance` is convenient but not production-grade: partial data, throttling, occasional revisions.
- Reported ROI ignores real-world frictions (spread, slippage, borrow, taxes) unless modeled.
- Daily equity direction is close to a martingale; not beating buy-and-hold is the *expected* result.
- This project is for education and portfolio purposes only. **Not financial advice.**

---

## Documentation

- `docs/adr/`: architecture decision records
- `docs/data_dictionary.md`: tables, columns, meanings
- `docs/runbook.md`: three most common failures
- `CHANGELOG.md`: notable changes

---

## Contributing

1. Fork and create a feature branch.
2. `pre-commit install`.
3. Use Conventional Commits.
4. `make lint` and `make test` must pass.
5. Open a pull request.

---

## Roadmap

- [ ] **M1:** Idempotent ingestion (retry, backoff, immutable raw Parquet layer)
- [ ] **M2:** dbt staging + marts with `not_null`, `unique`, `relationships`, source freshness
- [ ] **M3:** Data quality gate (Pandera); pipeline fails on bad data
- [ ] **M4:** Point-in-time features with anti-leakage tests
- [ ] **M5:** Walk-forward backtest, baseline vs main model, metrics + calibration
- [ ] **M6:** MLflow tracking
- [ ] **M7:** Prefect orchestration with alerts
- [ ] **M8:** Docker + `make up`
- [ ] **M9:** CI/CD: lint, pytest, `dbt build` on sample, merge blocking
- [ ] **M10:** Monitoring + Streamlit dashboard
- [ ] **M11:** Documentation: README, data dictionary, runbook, ADRs
- [ ] **M12:** Deployment + research-style summary

---

## License

This project is licensed under the [MIT License](LICENSE).
