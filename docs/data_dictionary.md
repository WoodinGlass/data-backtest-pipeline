# Data Dictionary

Every table, file, and artifact the pipeline produces, grouped by
layer. Types are SQL types (DuckDB dialect); filenames are paths
relative to the repo root unless stated otherwise.

**Reading order:** raw → staging → intermediate → marts → features →
artifacts. Each layer only depends on the one above it.

---

## Table of Contents

1. [Layers at a glance](#layers-at-a-glance)
2. [Raw layer](#1-raw-layer)
3. [Prices warehouse](#2-prices-warehouse)
4. [Macro warehouse](#3-macro-warehouse)
5. [Fundamental warehouse](#4-fundamental-warehouse)
6. [Features](#5-features)
7. [Backtest artifacts](#6-backtest-artifacts)
8. [Tracking (MLflow)](#7-tracking-mlflow)
9. [Monitoring](#8-monitoring)
10. [Orchestration](#9-orchestration)

---

## Layers at a glance

| Layer | Storage | Lifetime | Idempotency |
|---|---|---|---|
| Raw | Parquet + JSON manifest | Immutable, append-only | Content hash |
| Staging | DuckDB view | Rebuilt each dbt run | Deterministic |
| Intermediate | DuckDB table | Rebuilt each dbt run | Deterministic |
| Marts | DuckDB table | Rebuilt each dbt run | Deterministic |
| Features | Parquet (versioned) | Rebuilt per `FEATURE_VERSION` | Overwrite same version |
| Backtest artifacts | Parquet + JSON + PNG | Append-only per `run_id` | Run id |
| Tracking | SQLite + files | Append-only | MLflow run id |
| Monitoring | JSON | Overwrite | Report snapshot |

---

## 1. Raw layer

### `data/raw/prices/yfinance/<TICKER>/<date>__<hash16>.parquet`

One snapshot per ticker per fetch. Immutable, content-addressed.

| Column | Type | Description | Notes |
|---|---|---|---|
| `source` | TEXT | Data source identifier (e.g. `yfinance`) | Part of idempotency key |
| `ticker` | TEXT | Ticker symbol (e.g. `AAPL`) | Part of idempotency key |
| `trade_date` | DATE | The bar's date (UTC-normalized) | Part of idempotency key |
| `ingested_at` | TIMESTAMPTZ | When we fetched it (UTC) | Set by us |
| `payload_hash` | TEXT | SHA-256 of canonical OHLCV payload | Part of idempotency key |
| `open` | DECIMAL | Open price | **Hashed** |
| `high` | DECIMAL | High price | **Hashed** |
| `low` | DECIMAL | Low price | **Hashed** |
| `close` | DECIMAL | Close price | **Hashed** |
| `adj_close` | DECIMAL | Adjusted close, computed by the vendor | **Not hashed** — ADR 0006 |
| `volume` | BIGINT | Share volume | **Hashed**, non-negative |

**Hash definition:**

```
HASHED_COLUMNS = (open, high, low, close, volume)
PRICE_HASH_DECIMALS = 4
```

`adj_close` is stored but not hashed. It is vendor-derived and
recomputed on each fetch with tiny float noise (~3e-5). Hashing it
breaks idempotency; two snapshots with identical OHLCV but different
`adj_close` are the same bar semantically. See
`docs/adr/0006-content-hash-excludes-adjusted-close.md`.

### `data/raw/macro/fred/<SERIES_ID>/<vintage>__<hash16>.parquet`

One vintage snapshot of one FRED series. Vintage-aware (ALFRED).

| Column | Type | Description | Notes |
|---|---|---|---|
| `series_id` | TEXT | FRED series id (e.g. `FEDFUNDS`) | Part of idempotency key |
| `observation_date` | DATE | The period the value describes | |
| `value` | DOUBLE | Observed value; NULL for FRED's "." | |
| `vintage_date` | DATE | When this revision became current | Part of idempotency key |

See `docs/adr/0009-macro-data-design.md`.

### `data/raw/fundamentals/sec/<TICKER>/CIK#########__<hash16>.parquet`

One snapshot per ticker. Content-addressed by hash of the fact set.

| Column | Type | Description |
|---|---|---|
| `ticker` | VARCHAR | Ticker symbol (uppercase) |
| `cik` | BIGINT | SEC Central Index Key |
| `namespace` | VARCHAR | `us-gaap`, `dei`, `ifrs-full`, `ffd`, ... |
| `tag` | VARCHAR | XBRL tag name, e.g. `Revenues` |
| `unit` | VARCHAR | `USD`, `shares`, `pure`, ... |
| `period_start` | DATE | Period start; NULL for point-in-time facts |
| `period_end` | DATE | Period end |
| `filed` | DATE | Date the fact first appeared in a filing (PIT key) |
| `form` | VARCHAR | `10-K`, `10-Q`, `8-K`, ... |
| `fiscal_year` | INT | SEC fiscal year label, if present |
| `fiscal_period` | VARCHAR | `Q1`, `Q2`, `Q3`, `Q4`, `FY` |
| `frame` | VARCHAR | SEC calendar frame, if present |
| `value` | DOUBLE | Observed value; NULL for discontinued items |

See `docs/adr/0010-fundamental-data-design.md`.

### `data/raw/*/manifest.json`

One manifest per domain (prices, macro, fundamentals). Summarizes
snapshots already written so ingestion can skip unchanged payloads.

| Field | Type | Description |
|---|---|---|
| `version` | INT | Manifest schema version |
| `snapshots` | ARRAY | `{hash, path, n_rows, written_at, ...}` |
| `latest_*` | (varies) | Quick-lookup of newest snapshot per key |

---

## 2. Prices warehouse

### `staging.stg_prices`

Typed, renamed 1:1 view over the raw prices. Materialized as a view.

| Column | Type | Description |
|---|---|---|
| `ticker` | VARCHAR | Ticker symbol |
| `trade_date` | DATE | Trading date (renamed from `date`) |
| `open` | DOUBLE | Open |
| `high` | DOUBLE | High |
| `low` | DOUBLE | Low |
| `close` | DOUBLE | Close |
| `adj_close` | DOUBLE | Vendor-adjusted close |
| `volume` | BIGINT | Share volume |
| `source` | VARCHAR | Data source (e.g. `yfinance`) |
| `ingested_at` | TIMESTAMPTZ | Fetch timestamp |

### `intermediate.int_returns`

Log returns plus forward labels. **PIT contract:** features at `t`
use data available at `t`; labels at `t` describe the return from
`t` to `t+1`.

| Column | Type | Description |
|---|---|---|
| `ticker` | VARCHAR | Ticker symbol |
| `trade_date` | DATE | Trading date |
| `close` | DOUBLE | Close at `t` |
| `log_return` | DOUBLE | `ln(close_t / close_{t-1})` |
| `next_close` | DOUBLE | Close at `t+1` (for label computation) |
| `next_return` | DOUBLE | `ln(close_{t+1} / close_t)` |
| `next_return_positive` | BOOLEAN | `next_return > 0`; NULL at series end |

### `marts.dim_tickers`

Ticker metadata + point-in-time universe membership. Sourced from
`seeds/ticker_metadata.csv`.

| Column | Type | Description |
|---|---|---|
| `ticker` | VARCHAR | Ticker symbol |
| `sector` | VARCHAR | GICS-like sector label |
| `is_benchmark` | BOOLEAN | True for SPY |
| `valid_from` | DATE | First date this ticker is in the universe |
| `valid_to` | DATE | Last date; NULL = still active |

See `docs/adr/0005-universe-as-of-date.md`.

### `marts.fct_prices_daily`

Cleaned, typed, joined with sector + benchmark flag. Model-ready
OHLCV.

| Column | Type | Description |
|---|---|---|
| `ticker` | VARCHAR | Ticker symbol |
| `trade_date` | DATE | Trading date |
| `open` `high` `low` `close` `adj_close` | DOUBLE | OHLCV |
| `volume` | BIGINT | Share volume |
| `sector` | VARCHAR | From `dim_tickers` |
| `is_benchmark` | BOOLEAN | From `dim_tickers` |

### `marts.fct_returns_daily`

Model-ready: features at `t`, labels at `t+1`. Consumer of
`int_returns`.

| Column | Type | Description |
|---|---|---|
| `ticker` | VARCHAR | Ticker symbol |
| `trade_date` | DATE | Trading date |
| `close` | DOUBLE | Close at `t` |
| `log_return` | DOUBLE | Log return `t-1 → t` |
| `next_return` | DOUBLE | Log return `t → t+1` (label) |
| `next_return_positive` | BOOLEAN | Label for binary classification |
| `sector` | VARCHAR | From `dim_tickers` |
| `is_benchmark` | BOOLEAN | From `dim_tickers` |

---

## 3. Macro warehouse

### `staging.stg_macro_series`

Long format. Latest-mode series use `vintage_date = observation_date`
(PIT-effective). Full-vintage series preserve ALFRED's history.

| Column | Type | Description |
|---|---|---|
| `series_id` | VARCHAR | FRED series id |
| `observation_date` | DATE | Period the value describes |
| `vintage_date` | DATE | PIT-effective vintage date |
| `value` | DOUBLE | Observed value; NULL for FRED "." |

### `intermediate.int_macro_vintages`

One row per `(series_id, vintage_date)`. Collapses the staging table
for fast joins (23M → 280K rows).

| Column | Type | Description |
|---|---|---|
| `series_id` | VARCHAR | FRED series id |
| `vintage_date` | DATE | Vintage date |
| `max_obs` | DATE | Latest observation_date in this vintage |
| `value_at_max_obs` | DOUBLE | Value at `max_obs` |

### `intermediate.int_macro_daily`

PIT-correct daily panel: for each trade date, the vintage that was
current that day, forward-filled. Materialized as TABLE.

| Column | Type | Description |
|---|---|---|
| `trade_date` | DATE | Trade date |
| `series_id` | VARCHAR | FRED series id |
| `vintage_date` | DATE | Vintage current at `trade_date` |
| `observation_date` | DATE | Period the value describes |
| `value` | DOUBLE | Macro value (may be NULL) |

### `marts.fct_macro_daily`

Wide: one row per trade_date, 140 `mc_<series_id_lowercase>` columns.
Column list from the `macro_series_ids` dbt var. 141 columns total
(140 macro + trade_date).

See `docs/adr/0009-macro-data-design.md`.

---

## 4. Fundamental warehouse

### `staging.stg_sec_facts`

Typed SEC facts, 1:1 with raw. No filtering, no PIT enforcement at
this layer.

| Column | Type | Description |
|---|---|---|
| `ticker` | VARCHAR | Ticker symbol |
| `cik` | BIGINT | SEC Central Index Key |
| `namespace` | VARCHAR | `us-gaap`, `dei`, ... |
| `tag` | VARCHAR | XBRL tag name |
| `unit` | VARCHAR | `USD`, `shares`, ... |
| `period_start` | DATE | NULL for point-in-time facts |
| `period_end` | DATE | Period the fact describes |
| `filed` | DATE | Date the fact first appeared |
| `form` | VARCHAR | `10-K`, `10-Q`, `8-K`, ... |
| `fiscal_year` | INT | SEC fiscal year label |
| `fiscal_period` | VARCHAR | `Q1`..`Q4`, `FY` |
| `frame` | VARCHAR | SEC calendar frame |
| `value` | DOUBLE | May be NULL |

### `intermediate.int_fundamentals_pit`

PIT join: for each `(ticker, trade_date, namespace, tag)`, the value
from the latest formal filing (10-K / 10-Q) with `filed <= trade_date`
and `period_end <= filed`. Materialized as TABLE (12.5M rows).
Partitioned per year via UNION to bound memory.

| Column | Type | Description |
|---|---|---|
| `trade_date` | DATE | Trading date |
| `ticker` | VARCHAR | Ticker symbol |
| `namespace` | VARCHAR | `us-gaap` or `dei` |
| `tag` | VARCHAR | XBRL tag name (from curated registry) |
| `value` | DOUBLE | Value as of `trade_date`; NULL if no filing yet |
| `filed_used` | DATE | Filing date of the fact producing this value |
| `period_end_used` | DATE | Period the value describes |

See `docs/adr/0010-fundamental-data-design.md` and
`docs/adr/0011-memory-management-duckdb.md`.

---

## 5. Features

### `data/features/v1/features_daily.parquet`

Point-in-time feature table. Versioned by `FEATURE_VERSION`
(`features/config.py`); bumping the version creates a new directory.

| Column | Type | Description |
|---|---|---|
| `ticker` | VARCHAR | Ticker symbol |
| `trade_date` | DATE | Trading date (`t`) |
| `px_return_1d` `px_return_5d` `px_return_20d` | DOUBLE | Lagged log returns |
| `px_vol_20d` `px_vol_60d` | DOUBLE | Rolling volatility (annualized) |
| `px_rsi_14` | DOUBLE | RSI(14) |
| `px_momentum_60d` | DOUBLE | 60-day momentum |
| `px_volume_ratio_5_20` | DOUBLE | 5d / 20d mean volume ratio |
| `cs_percentile_rank_sector_20d` | DOUBLE | Cross-sectional rank within sector |
| `cs_percentile_rank_universe_20d` | DOUBLE | Cross-sectional rank across universe |
| `mkt_beta_60d` | DOUBLE | Rolling 60-day beta vs SPY |
| `mkt_corr_60d` | DOUBLE | Rolling 60-day correlation vs SPY |
| `mc_fedfunds` `mc_dgs10` `mc_dgs2` | DOUBLE | Rates (from `fct_macro_daily`) |
| `mc_cpi_yoy` `mc_payrolls_yoy` `mc_unemployment` | DOUBLE | Macro indicators |
| `fd_net_margin` `fd_roe` | DOUBLE | Profitability (from `int_fundamentals_pit`) |
| `fd_capex_intensity` | DOUBLE | Capex / revenue |
| `fd_revenue_yoy` | DOUBLE | Revenue growth (ASC 606 tags coalesced) |
| `fd_employees` | DOUBLE | Headcount (from 10-K text) |
| `next_return` | DOUBLE | Label: log return `t → t+1` |
| `next_return_positive` | BOOLEAN | Binary label for classification |

23 features across 5 families (`px_*`, `cs_*`, `mkt_*`, `mc_*`,
`fd_*`). See `docs/adr/0012-feature-engineering-architecture.md`.

---

## 6. Backtest artifacts

Produced per run in `data/backtest/<run_id>/`, where `run_id` is
`YYYYMMDD_HHMMSS_<model-tag>`.

### `config.json`

Snapshot of `BacktestSettings` + `RiskSettings` used for this run.

### `split.json`

Fold boundaries. One entry per fold with
`train_start`, `train_end`, `test_start`, `test_end`.

### `predictions/fold_<NN>.parquet`

One file per fold. Long format with
`ticker`, `trade_date`, `prob`, `next_return`, `y_true`.

### `returns/fold_<NN>.parquet`

One file per fold. Daily portfolio returns:
`gross`, `cost`, `net`, `cash_ret`, indexed by `trade_date`.

### `metrics.parquet`

Per-fold metrics table. Columns:
`model`, `fold_id`, `test_start`, `test_end`, `cls_*`, `trd_*`.

### `summary.json`

Full aggregated report: median+IQR + mean+std per metric, pooled
metrics, bootstrap CI, deflated Sharpe, yearly Sharpe.

### `table_pooled.parquet`, `table_deflated.parquet`, `table_aggregate.parquet`, `table_per_fold.parquet`

Compact per-model tables for quick display (dashboard, reports).

### `equity_curves.png`, `calibration.png`

Pre-rendered plots (matplotlib). Equity curve: main + baselines +
SPY. Calibration: pooled main model, 10 bins.

See `docs/adr/0014-walk-forward-methodology.md`.

---

## 7. Tracking (MLflow)

### `mlruns/mlflow.db`

SQLite tracking + model registry store. Git-ignored.

### `mlruns/<experiment_id>/<run_id>/`

One directory per MLflow run. Contains:

- `params/`, `metrics/`, `tags/` (metadata)
- `artifacts/backtest/` — a copy of `data/backtest/<run_id>/`
- `artifacts/meta/*.diff` — uncommitted git diff at run time
- `artifacts/meta/*.txt` — `pip freeze` snapshot
- `artifacts/model/` — for registry-purpose runs, the sklearn pipeline

**Experiment:** `daily-direction`.
**Registry model name:** `daily-direction-model`.
**Aliases:** `@challenger` (auto), `@champion` (manual).

See `docs/adr/0015-mlflow-tracking.md`.

---

## 8. Monitoring

### `reports/monitoring.json`

Snapshot of the last `make monitor` run. Top-level shape:

| Key | Type | Description |
|---|---|---|
| `generated_at` | str | ISO 8601 UTC |
| `version` | str | Monitoring schema version |
| `reference_date` | str | "today" as seen by freshness |
| `freshness` | obj | Per-mart status (PASS / WARN / FAIL) |
| `drift` | obj | PSI + KS per numeric feature |
| `performance` | obj | Rolling metrics from the latest backtest |
| `errors` | arr | Sections that failed to build |
| `overall` | str | Worst of the three sections |

See `docs/adr/0019-monitoring-dashboard.md`.

### `reports/quality.json`

Quality gate report. Keys: `layer` (raw / staging / marts),
`checks` (list with `name`, `passed`, `rows_checked`, `violations`),
`passed` (bool). Written by `python -m quality.cli --json`.

---

## 9. Orchestration

Prefect flows do **not** create their own tables or files. They call
the same CLIs that a human would, so every artifact listed above is
produced by an orchestrated run identically to a manual one.

Flow run metadata lives in Prefect's own store
(`~/.prefect/prefect.db` locally, or the `prefect-server` container
in served mode). See `docs/adr/0016-prefect-orchestration.md`.
