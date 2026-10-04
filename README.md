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
- A portfolio project that demonstrates end-to-end data engineering and ML evaluation practices on **US equity daily data**, enriched with **macro** and **fundamental** context.
- A reproducible pipeline: public data sources → immutable raw layer → tested dbt models → point-in-time features → walk-forward backtest → monitored dashboard.
- An example of how to evaluate probabilistic models honestly (log loss, Brier score, calibration, Sharpe, max drawdown) **without data leakage, survivorship bias, or look-ahead bias**.
- A demonstration that a small, well-built model **often fails to beat buy-and-hold** — and that reporting that honestly is the correct engineering outcome.

**This is not:**
- A trading bot. It does not place orders through any broker.
- Financial advice. Backtest results do not guarantee future performance.
- A real-time or intraday system. Ingestion is **batch** by design.
- A high-frequency or alpha-generating strategy. Signals in daily equity data are weak and noisy by nature; the point is the pipeline, not the alpha.
- A general-purpose ML platform. Scope is limited to daily US equities, macro, and fundamentals for one pipeline.

---

## Problem

Backtests are often unreliable because of hidden data leakage, **survivorship bias**, **look-ahead bias in macro and fundamental data**, non-reproducible data, and uncalibrated probabilities. This project builds a pipeline where every number in a backtest can be traced back to immutable raw data, tested transformations, and a versioned model.

> One-sentence problem statement: **How can we build a reproducible, leakage-free pipeline that turns daily US equity prices — enriched with vintage-aware macro and filing-date-aware fundamental data — into honestly evaluated probabilistic direction predictions, and monitors them over time?**

**Target users:** data engineers, ML engineers, and reviewers who want to see production-style practices applied to financial time-series in a compact project.

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Domain | **US equities, S&P 500 subset (31 tickers + SPY), daily bars** | Clean data, standard benchmarks, honest evaluation |
| Payload vs pipeline | **Pipeline is the product**; the model is the payload | Focus is on reliability, testing, and reproducibility |
| Deterministic parts | Ingestion (given cached raw), dbt models, feature builders, backtest | Same input → same output; hash of input is the idempotency key |
| Non-deterministic parts | Model training (seeded), upstream revisions | Seeds are fixed; raw is stored immutably |
| Storage tier | DuckDB (local warehouse) + Parquet (raw/artifacts); Snowflake optional | Small structured data, analytical queries |
| Ingestion mode | **Batch, daily** | Simple, cheap, sufficient for daily bars |
| Macro data | **FRED + ALFRED, vintage-aware** | PIT-correct backtests; see ADR 0009 |
| Fundamental data | **SEC EDGAR XBRL, filing-date PIT** | Primary source, no look-ahead; see ADR 0010 |
| Data quality | **Three-layer defense: ingest / gate / dbt** | Each rule lives in exactly one layer; see ADR 0008 |
| Memory strategy | **Partition + arg_max + SQL sampling** for large models | See ADR 0011 |
| Schema failures | **Hard fail** | Bad schema means corrupt data |
| Freshness/volume issues | **Warn first**, then fail after threshold | Recoverable conditions |
| Config | Environment variables + typed settings | No hardcoded paths or backends |
| Prediction target | `P(return_{t+1} > 0)` per ticker-day | Binary, calibratable, comparable across tickers |
| Primary benchmark | **Buy-and-hold SPY** | The honest baseline nobody beats consistently |

Full rationale is recorded in `docs/adr/`.

---

## Architecture

```text
   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
   │  yfinance    │    │  FRED +      │    │  SEC EDGAR   │
   │  (prices)    │    │  ALFRED      │    │  (XBRL)      │
   │              │    │  (macro)     │    │ (fundamental)│
   └──────┬───────┘    └──────┬───────┘    └──────┬───────┘
          │                   │                   │
          ▼                   ▼                   ▼
   ┌─────────────────────────────────────────────────────┐
   │  ingestion/  ·  retry, backoff, idempotent writes   │
   │  immutable Parquet, content-addressed               │
   └──────────────────────┬──────────────────────────────┘
                          ▼
   ┌─────────────────────────────────────────────────────┐
   │  raw layer   immutable, append-only                 │
   │   data/raw/prices/  |  macro/  |  fundamentals/     │
   └──────────────────────┬──────────────────────────────┘
                          ▼
   ┌─────────────────────────────────────────────────────┐
   │  dbt   staging → intermediate → marts               │
   │   prices:      stg_prices      → fct_returns_daily  │
   │   macro:       stg_macro       → fct_macro_daily    │
   │   fundamental: stg_sec_facts   → fct_fundamentals   │
   └──────────────────────┬──────────────────────────────┘
                          ▼
   ┌─────────────────────────────────────────────────────┐
   │  Data quality gate (Pandera, 8 layers)              │
   └──────────────────────┬──────────────────────────────┘
                          ▼
   ┌─────────────────────────────────────────────────────┐
   │  features/   point-in-time builders                 │
   │   + anti-leakage tests                              │
   └──────────────────────┬──────────────────────────────┘
                          ▼
   ┌──────────────┐    ┌──────────┐
   │  backtest/   │───▶│  MLflow  │  params, metrics, artifacts
   └──────┬───────┘    └──────────┘
          ▼
   ┌──────────────┐
   │  monitoring  │  drift, freshness, model performance
   └──────┬───────┘
          ▼
   ┌──────────────┐
   │  Streamlit   │  dashboard
   └──────────────┘

   Orchestration: Prefect (scheduled flows + failure alerts)
```

---

## Tech Stack

- **Language:** Python 3.11
- **Market data:** `yfinance` (default, no API key), optional `Stooq` fallback
- **Macro data:** FRED + ALFRED (vintage-aware, 140 curated series)
- **Fundamental data:** SEC EDGAR XBRL `companyfacts` API (137 curated tags)
- **Warehouse:** DuckDB (default), Snowflake (optional)
- **Transformation:** dbt (with `dbt_utils`)
- **Data quality:** Pandera, dbt tests
- **Orchestration:** Prefect
- **Experiment tracking:** MLflow
- **Containers:** Docker, docker-compose
- **CI/CD:** GitHub Actions
- **Dashboard:** Streamlit
- **Tooling:** ruff, mypy, pytest, pre-commit, Makefile

---

## Data Contracts

Schemas are defined before any producer or consumer code is written.

**Prices (`ingestion/schemas.py`):**

```python
class RawPriceEvent(BaseModel):
    """One immutable daily OHLCV bar for a single ticker."""

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
```

**Macro (`ingestion/macro/schemas.py`):** `MacroSnapshot` — one row per
`(series_id, observation_date, vintage_date)`, with content-hash
idempotency that excludes `fetched_at`. See ADR 0009.

**Fundamental (`ingestion/sec/schemas.py`):** `SecFact` — one row per
`(ticker, namespace, tag, period_end, filed, form, frame)`, with
`filed` as the PIT key. See ADR 0010.

Contracts live in `ingestion/schemas.py`,
`ingestion/macro/schemas.py`, `ingestion/sec/schemas.py` and are
documented in `docs/data_dictionary.md`.

The raw Parquet snapshots additionally carry a `ticker` / `series_id`
column written by the store so that dbt models do not need to infer
identity from the file path. The content hash is unaffected.

---

## Pipeline Layers

### 1. Ingestion (`ingestion/`)
- Batch pull from `yfinance`, FRED/ALFRED, and SEC EDGAR with retries
  and exponential backoff with jitter.
- Idempotent writes; every raw snapshot is content-addressed.
- Corrections (splits, dividends, restatements, revised vintages) are
  **new rows**, never updates.

### 2. Warehouse — Prices (`dbt/`)
- **`staging`** — `stg_prices`: typed, renamed OHLCV 1:1 with raw.
- **`intermediate`** — `int_returns`: log returns plus forward labels
  (next-day close, log return, sign). PIT contract enforced by
  singular tests.
- **`marts`** — `dim_tickers`, `fct_prices_daily`, `fct_returns_daily`
  (model-ready: features at `t`, labels at `t+1`).

### 3. Macro warehouse (`ingestion/macro/` + `dbt/`)
- **Ingestion** — FRED + ALFRED client with retry, backoff, rate limit.
- **Curation** — `config/macro_series.yml`: 140 series across 10
  categories (monetary policy, rates, inflation, employment, growth,
  money & credit, housing, energy, sentiment, international).
- **`staging.stg_macro_series`** — long format. Latest-mode series
  (45 total: daily rates, exchange rates, commodity spot prices, and
  non-revised daily indices) use `vintage_date = observation_date`.
  Full-mode series (95) preserve FRED's vintage history.
- **`intermediate.int_macro_vintages`** — one row per
  `(series_id, vintage_date)`, collapsing the staging table for fast
  joins.
- **`intermediate.int_macro_daily`** — PIT join by trade date.
  Materialized as TABLE. Uses ASOF-decomposition for speed.
- **`marts.fct_macro_daily`** — wide format, one column per series
  (141 columns total).
- See **ADR 0009**.

### 4. Fundamental warehouse (`ingestion/sec/` + `dbt/`)
- **Ingestion** — SEC EDGAR `companyfacts` API client, per-ticker.
- **Curation** — `config/fundamental_tags.yml`: 137 unique XBRL tags
  across 6 categories.
- **Skip list** — `config/sec_skip_tickers.yml` marks ETFs and other
  non-company tickers (currently SPY).
- **`staging.stg_sec_facts`** — typed SEC facts, 1:1 with raw.
- **`intermediate.int_fundamentals_pit`** — PIT join with `filed <=
  trade_date`. Split per year via UNION to bound memory. Materialized
  as TABLE (12.5M rows).
- See **ADR 0010** and **ADR 0011**.

### 5. Data quality gate (`quality/`)
- **8 declarative Pandera schemas** at layer boundaries:
  - `RawPricesSchema`, `StgPricesSchema`, `FctReturnsSchema`
  - `SecFactsSchema`
  - `StgMacroSeriesSchema`, `IntMacroDailySchema`
  - `StgSecFactsSchema`, `IntFundamentalsPitSchema`
- **Runner** (`quality/gate.py`) loads each layer, validates, produces
  a structured JSON report, and exits non-zero on any failure.
  Large tables are sampled at the SQL level (200K rows) so the gate
  stays fast (≈40s for all 39 checks). Full row counts are still
  reported.
- **CLI** (`dbp-quality`, `make quality`) for local, CI, and
  orchestration use.
- **Three-layer defense** (see ADR 0008):
  1. `ingestion/validation.py` — vectorized, in the ingest hot path.
  2. `quality/` — declarative contracts at layer boundaries.
  3. `dbt/models/**/*.yml` + `dbt/tests/` — SQL-native warehouse checks.
- Failures are actionable: the report identifies the layer, the row,
  the column, and the invariant that was violated.

### 6. Features (`features/`)
- **Point-in-time feature builders** as pure functions: input
  DataFrame in, output DataFrame out, no IO. Assembled by
  `features/assembler.py`, written to versioned Parquet at
  `data/features/v1/features_daily.parquet`.
- **23 features across 5 families** (v1):
  - `px_*` (8) — lagged returns 1/5/20d, volatility 20/60d, RSI(14),
    momentum 60d, volume ratio 5/20.
  - `cs_*` (2) — cross-sectional percentile ranks within sector and
    within the universe, at 20d horizon.
  - `mkt_*` (2) — 60d beta and correlation vs SPY.
  - `mc_*` (6) — Fed funds, DGS10, DGS2, CPI YoY, payrolls YoY,
    unemployment rate. Joined from vintage-aware
    `marts.fct_macro_daily`.
  - `fd_*` (5) — net margin, ROE, capex intensity, revenue YoY,
    employees. Joined from filing-date PIT
    `intermediate.int_fundamentals_pit`, with ASC 606 revenue tags
    coalesced.
- **Anti-leakage tests** for every family: poison future rows with
  garbage, recompute, assert features at `t` unchanged. See
  `tests/unit/test_*_features.py`.
- **Versioned output.** Bumping `FEATURE_VERSION` in
  `features/config.py` creates a new directory; old versions stay.
  See ADR 0012.

### 7. Backtest (`backtest/`) — planned M5
- Walk-forward evaluation with expanding or rolling windows.
- Baselines: 50/50 naive, momentum, buy-and-hold SPY.
- Main model: calibrated classifier.
- Metrics: log loss, Brier, calibration, hit rate, Sharpe, max
  drawdown, ROI vs SPY.

### 8. Models (`models/`) — planned M5–M6
- Train, calibrate, and register models. Every run tracked in MLflow.

### 9. Orchestration (`orchestration/`) — planned M7
- Prefect flows on a daily schedule after US market close.

### 10. Monitoring (`monitoring/`) — planned M10
- Data freshness, feature drift, prediction drift, rolling model
  performance vs buy-and-hold.

### 11. Dashboard (`app/`) — planned M10
- Streamlit: pipeline health, backtest results, calibration, equity
  curve vs SPY, drift.

---

## Idempotency and Failure Modes

**Idempotency:** running any write step twice must produce the same
final state.

| Operation | Key | Strategy |
|---|---|---|
| Insert raw prices | `(source, ticker, trade_date, payload_hash)` | Content-addressed |
| Insert raw macro | `(series_id, observation_date, vintage_date, hash)` | Content-addressed |
| Insert raw fundamental | `(ticker, cik, hash)` | Content-addressed |
| Refetch after revision | New hash | New immutable row |
| Feature build | `(feature_version, ticker, date)` | Overwrite the same partition |
| Model run | Run ID | Tracked in MLflow |

**Failure modes:**

| What can fail | Impact | Strategy |
|---|---|---|
| `yfinance` returns empty / partial | Missing bars | Retry with jitter, use cached raw, freshness warning |
| `yfinance` rate-limits | Ingestion delayed | Backoff, batch tickers across time |
| FRED / ALFRED unavailable | Macro stale | Retry with jitter; freshness check alerts |
| SEC EDGAR rate-limits (>10 req/s) | Fundamental delayed | Politeness headers, 6.7 req/s cap |
| Upstream price revision | Inconsistent series | Store as new row; recompute in dbt |
| Macro revision (vintage) | PIT divergence | Store new vintage; PIT join picks correct one |
| Fundamental restatement | Restated facts | Store new filing; PIT join uses `filed <= trade_date` |
| Ticker delisted | **Survivorship bias** | Universe defined as-of date, not as-of today |
| Warehouse | Pipeline stops | Retry, fail loudly, alert |
| Bad data | Wrong returns | DQ gate fails the run |
| Large query OOM | Pipeline killed | Partition + arg_max + SQL sampling (ADR 0011) |
| Network | Intermittent errors | Retry with jitter |

**Deliberate non-strategy:** we do **not** silently forward-fill
missing prices, we do **not** drop delisted tickers, and we do **not**
use current (revised) macro values in a historical backtest.

---

## Universe & Data Source

**Universe:** 31 large-cap, highly liquid S&P 500 tickers + SPY
benchmark.

Defined in four places:
- `config/universe.txt` — what ingestion fetches.
- `dbt/seeds/ticker_metadata.csv` — sectors, benchmark flag, and
  point-in-time validity intervals (`valid_from`, `valid_to`). See
  ADR 0005.
- `config/macro_series.yml` + `config/macro_series_latest_only.yml` —
  140 FRED/ALFRED series; 95 full-vintage, 45 latest-mode.
- `config/fundamental_tags.yml` + `config/sec_skip_tickers.yml` —
  137 XBRL tags; SPY excluded.

**Benchmark:** `SPY`.

**Data sources:**

| Layer         | Source        | Frequency | Vintage handling                  |
|---------------|---------------|-----------|-----------------------------------|
| Prices        | yfinance      | Daily     | Append-only (no vintage)          |
| Macro         | FRED + ALFRED | Monthly+  | Per-release vintage; latest-mode  |
| Fundamentals  | SEC EDGAR     | Quarterly | Per filing (`filed` PIT)          |

All three sources are free, require no paid subscription, and are
cached to Parquet immediately so the pipeline is reproducible
without network access after the initial ingest.

**Frequency:** prices fetched daily after US market close. Macro and
fundamental ingestion run on a weekly schedule (they update less
often than daily).

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
make install        # install dependencies
make install-all    # install all extras (Colab/dev recommended)
make lint           # ruff + mypy (whole repo)
make test           # unit tests
make test-int       # integration tests
make ingest         # fetch daily prices into the raw layer
make ingest-macro   # fetch macro series (M3.5)
make ingest-sec     # fetch fundamental filings (M3.7)
make dbt-build      # run dbt models and tests
make quality        # run the data quality gate (8 layers)
make ci             # run the full CI suite locally
make setup-dev      # one-command resumable environment setup
make features       # build the feature table (M4)
make backtest       # run the walk-forward backtest
make app            # start the Streamlit dashboard
make down           # stop services
```

**Colab users:** run `scripts/setup_dev.py` (or `make setup-dev`).
It is idempotent and resumable: state is checkpointed to
`data/.setup_state.json`, so a restart during the ~2 hour macro
ingest does not force a full re-run.

---

## Configuration

All configuration is via environment variables. See `.env.example`.

```bash
# Warehouse
WAREHOUSE_BACKEND=duckdb
DUCKDB_PATH=./data/warehouse.duckdb

# Prices
PRICE_SOURCE=yfinance
UNIVERSE_FILE=./config/universe.txt
BENCHMARK_TICKER=SPY
PRICE_HISTORY_START=2015-01-01

# Macro (M3.5)
FRED_API_KEY=changeme
MACRO_SERIES_FILE=./config/macro_series.yml
MACRO_HISTORY_START=2015-01-01

# Fundamentals (M3.7)
SEC_USER_AGENT="data-backtest-pipeline you@example.com"
FUNDAMENTAL_TAGS_FILE=./config/fundamental_tags.yml
FUNDAMENTAL_HISTORY_START=2015-01-01

# MLflow
MLFLOW_TRACKING_URI=./mlruns

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=json
```

Dependencies live in `pyproject.toml` with self-contained extras:
`dev`, `dbt`, `snowflake`, `integration`, `observability`,
`orchestration`, `tracking`, `quality`, `app`.

---

## Project Structure

```text
├── ingestion/          # price client (yfinance)
│   ├── macro/          # FRED + ALFRED client (M3.5)
│   └── sec/            # SEC EDGAR XBRL client (M3.7)
├── quality/            # Pandera schemas + gate + CLI (dbp-quality)
├── dbt/
│   ├── dbt_project.yml     # project config + vars
│   ├── packages.yml        # dbt_utils and other deps
│   ├── profiles.example.yml
│   ├── seeds/              # ticker_metadata.csv
│   ├── models/
│   │   ├── staging/        # stg_prices, stg_macro_series, stg_sec_facts
│   │   ├── intermediate/   # int_returns, int_macro_vintages,
│   │   │                   # int_macro_daily, int_fundamentals_pit
│   │   └── marts/          # dim_tickers, fct_returns_daily,
│   │                       # fct_prices_daily, fct_macro_daily
│   ├── tests/              # singular tests (PIT, OHLC, benchmark)
│   └── macros/             # generate_schema_name, freshness
├── features/           # PIT feature builders (M4) + versioned Parquet
├── backtest/           # walk-forward, metrics, staking
├── models/             # train, calibrate, registry
├── orchestration/      # Prefect flows
├── monitoring/         # drift, freshness, model performance
├── app/                # Streamlit dashboard
├── config/             # universe.txt, macro_series*.yml,
│                       # fundamental_tags.yml, sec_skip_tickers.yml
├── scripts/            # setup_dev.py, checkpoint.py, fixtures,
│                       # cleanup_*_raw.py, sync_*_var.py
├── tests/
│   ├── unit/           # pure, no external services
│   ├── integration/    # needs network or a warehouse
│   └── fixtures/       # committed tiny Parquet fixture for CI
├── docs/               # ADR 0001-0011, data dictionary, runbook
├── .github/workflows/  # CI: lint-and-test + dbt-build + quality
├── Dockerfile  docker-compose.yml  Makefile
└── pyproject.toml  .pre-commit-config.yaml
```

---

## Testing Strategy

| Tier | Count | Scope | Marker |
|---|---|---|---|
| Unit (pytest) | ~290 | Pure functions, no external services (default) | none |
| Integration (pytest) | 10 | Needs network or a warehouse | `@pytest.mark.integration` |
| dbt tests (schema + singular) | ~99 | Column-level + SQL checks | — |
| Slow | — | Long-running backtests | `@pytest.mark.slow` |

Key tests:
- **Anti-leakage (prices):** features for date `t` never change when future rows are shuffled.
- **Anti-leakage (macro):** no value in `int_macro_daily` originates from a vintage later than the trade date.
- **Anti-leakage (fundamental):** no fact originates from a filing whose `filed > trade_date`.
- **Survivorship:** universe membership is date-aware; delisted tickers remain.
- **Idempotency:** running ingestion twice does not duplicate rows.
- **dbt tests:** `not_null`, `unique`, `relationships`, source freshness.
- **Quality gate:** corrupting a copy of the warehouse triggers failure with an actionable message.
- **Metrics:** log loss, Brier, Sharpe, max drawdown checked against hand-computed values.
- **Corporate actions:** a synthetic 2:1 split does not produce a spurious −50% return.

---

## CI/CD

GitHub Actions runs on every push and pull request. Two jobs run in
parallel:

**`lint-and-test`** (Python):
1. Install dependencies (`pip install -e ".[dev,quality]"`)
2. Lint (ruff) and format check
3. Type-check (mypy, strict)
4. Unit tests (`pytest -m "not integration and not slow"`)

**`dbt-build`** (SQL / warehouse):
1. Install `dbt-core` + `dbt-duckdb` + Pandera
2. `dbt deps` (install `dbt_utils`)
3. `dbt build` **against committed fixtures**
   (`tests/fixtures/raw/prices/yfinance/` and
   `tests/fixtures/raw/macro/fred/`)
4. **Quality gate** (`python -m quality.cli --tickers-from-raw
   --skip sec --skip stg_sec --skip int_fundamentals --skip stg_macro
   --skip int_macro`) validates what the fixture covers
5. Quality report uploaded as a CI artifact

The fixtures are tiny, deterministic subsets of the raw layers (3
tickers × 21 days; 4 macro series). CI is fully hermetic: no network,
no yfinance, no FRED, no SEC EDGAR, identical results on every run.

Merges are blocked if either job fails. Commits follow
[Conventional Commits](https://www.conventionalcommits.org/).

---

## Observability and Monitoring

- **Structured JSON logs** with correlation IDs.
- **Data freshness:** alert when latest `trade_date` in marts is
  stale; macro and fundamental freshness tracked separately.
- **Drift:** feature distribution shift vs training window.
- **Model performance:** rolling log loss, Brier, hit rate, Sharpe,
  cumulative return vs SPY.
- **Failure alerts:** sent from Prefect flows.

Severity policy: schema violation → hard fail; freshness/volume →
warning first; integrity check → soft fail.

---

## Results

> To be filled in after M5 and M12. Claims about performance require
> numbers.

| Model | Log loss | Brier | Hit rate | Sharpe | Max DD | ROI vs SPY |
|---|---|---|---|---|---|---|
| Naive 50/50 | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| Momentum (last return) | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| Buy-and-hold SPY | — | — | — | [ ] | [ ] | 0.00 |
| Prices-only model (calibrated) | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| Prices + macro + fundamental | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |

- Calibration curve: `docs/preview/calibration.png`
- Cumulative returns vs SPY: `docs/preview/equity_curve.png`

**Honest reporting policy:** if the main model does not beat
buy-and-hold, this table will say so. Likewise, if adding macro and
fundamental features does not improve the model, that result will be
reported — it is the expected outcome in daily equity direction
prediction.

---


---

## M4.5 — Risk framework in one page

Locked contract: **ADR 0013**. All parameters live in `risk/config.py` and are
env-overridable (`DBP_RISK_*`).

| Decision | Value | Rationale |
|---|---|---|
| Staking | **Quarter-Kelly** (`k=0.25`), cap **5% NAV** | Full Kelly over-bets on noisy `p`; quarter-Kelly tolerates ~2× estimation error |
| Vol targeting | 10% annual, 20d lookback, `min(1, target/realized)` | Only shrinks — no leverage-up in calm regimes |
| Direction | Long + flat (no short) | Short requires borrow model + squeeze handling (M5.5) |
| Stop loss | -8% per position, 5d cooldown | ~2.5 ATR large-cap |
| Drawdown | derisk 50% at -10%, halt at -20% | Soft then hard circuit breaker |
| Idle cash | Fed Funds (`mc_fedfunds`) | More honest than 0% |
| Costs | 2.5 bp one-way (5 bp round-trip) | Realistic for liquid large-cap |
| Rebalance | Threshold 1% NAV | Reduces churn |

### Modules

- `risk/config.py` — `RiskSettings` (23 fields, 2 derived props, 1 cross-field validator)
- `risk/staking.py` — quarter-Kelly, fixed-fractional, equal-weight, vol-target
- `risk/entry.py` — threshold, top-N, cross-sectional (benchmark excluded)
- `risk/limits.py` — per-position stop-loss, cooldown, DD derisk, DD halt

### Test coverage

| File | Tests | Focus |
|---|---|---|
| `test_risk_config.py` | 24 | Defaults, derived props, validator, env override |
| `test_risk_staking.py` | 21 | All four staking rules, purity, error cases |
| `test_risk_entry.py` | 23 | Threshold, top-N, cross-sectional, benchmark exclusion |
| `test_risk_limits.py` | 21 | Stop-loss, DD derisk/halt (both scenarios), row-order preservation |
| `test_risk_integration.py` | 15 | **Anti-look-ahead** (prefix stability + poison future), composition |
| **Total** | **104** | |

## Limitations

- Backtests rely on historical data and cannot capture regime
  changes, structural breaks, or future innovations.
- **Survivorship, look-ahead, and vintage bias are enemies #1, #2,
  and #3.** We address all three explicitly, but no mitigation is
  perfect.
- `yfinance` is convenient but not production-grade: partial data,
  throttling, occasional revisions.
- FRED and ALFRED occasionally correct their own archives; we treat
  corrections as new snapshots.
- **Macro ingest remains slow** (~15 min full universe) even after
  promoting 45 daily series to latest-mode. The remaining cost is the
  95 full-vintage series, which is the correct trade-off — those
  series are actually revised.
- SEC EDGAR XBRL coverage varies by sector. Some tags (e.g.
  `InventoryNet`) apply to retailers but not banks. Downstream
  features must tolerate NULLs.
- **XBRL tag stability varies across eras.** Revenue tags changed
  around 2018 with ASC 606; feature builders must coalesce multiple
  tags for a single semantic series.
- Employee count extraction from 10-K text is best-effort; coverage
  target is 60–80%, reported by the quality gate.
- **XOM history is limited** — its ticker currently resolves to a
  2024 reorganization entity with facts only from mid-2026. Union of
  pre- and post-reorg CIKs is future work.
- Reported ROI ignores real-world frictions (spread, slippage,
  borrow, taxes) unless modeled.
- Daily equity direction is close to a martingale; not beating
  buy-and-hold is the *expected* result.
- This project is for education and portfolio purposes only. **Not
  financial advice.**

---

## Documentation

- `docs/adr/`: architecture decision records (0001–0011)
- `docs/data_dictionary.md`: tables, columns, meanings
- `docs/runbook.md`: three most common failures
- `CHANGELOG.md`: notable changes

---

## Contributing

1. Fork and create a feature branch.
2. `pre-commit install`.
3. Use Conventional Commits.
4. `make lint` and `make test` must pass. For data-layer changes,
   `make quality` must also pass.
5. Open a pull request.

---

## Roadmap

Progress: **11 / 15 milestones selesai (~73%)**. Fokus berikutnya: **M5 (walk-forward backtest)**.

### ✅ Selesai

- [x] **M1:** Idempotent ingestion (retry, backoff, immutable raw Parquet layer)
- [x] **M2:** dbt staging + marts with `not_null`, `unique`, `relationships`, source freshness
- [x] **M3:** Data quality gate (Pandera) — pipeline fails on bad data
- [x] **M3.5:** Macro ingestion (FRED + ALFRED, vintage-aware series)
- [x] **M3.6:** Macro warehouse (staging, daily PIT forward-fill, marts)
- [x] **M3.7:** Fundamental ingestion (SEC EDGAR XBRL, curated tags)
- [x] **M3.8:** Fundamental warehouse (staging, filing-date PIT join)
- [x] **M3.9:** Quality gate extension for macro + fundamental layers
- [x] **M3.9.5:** Daily-series optimization (latest-mode expansion)
- [x] **M4:** Point-in-time features (prices + macro + fundamental), anti-leakage tests
- [x] **M4.5:** Risk framework — staking (quarter-Kelly), entry rules, stop-loss, drawdown halt (ADR 0013)

### 🚧 Berikutnya

- [ ] **M5:** Walk-forward backtest, baseline vs main model, metrics + calibration
- [ ] **M6:** MLflow tracking (parameters, metrics, artifacts, model versions)
- [ ] **M7:** Prefect orchestration with failure alerts
- [ ] **M8:** Docker + `make up` for one-command reproducibility
- [ ] **M9:** CI/CD: lint, pytest, `dbt build` on sample, merge blocking
- [ ] **M10:** Monitoring (drift, freshness, model performance) + Streamlit dashboard
- [ ] **M11:** Documentation: README, data dictionary, runbook, ADRs
- [ ] **M12:** Deployment (Streamlit Cloud/VPS) + research-style results summary

## License
## License

This project is licensed under the [MIT License](LICENSE).
