# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial repository skeleton: folder structure, tooling, CI stub.
- Foundational files: `.gitignore`, `LICENSE` (MIT), `pyproject.toml`, `.env.example`.
- Architecture Decision Records (ADR 0001-0006).
- Data dictionary and runbook stubs.
- **M1: idempotent ingestion pipeline.**
  - `ingestion/schemas.py` — `RawPriceEvent` contract + payload hashing.
  - `ingestion/logging.py` — structured JSON logs with correlation IDs.
  - `ingestion/config.py` — typed settings via pydantic-settings.
  - `ingestion/client.py` — yfinance-backed `PriceSource` with retry,
    backoff, and rate limiting.
  - `ingestion/raw_store.py` — immutable, content-addressed Parquet
    snapshots with a JSON manifest.
  - `ingestion/pipeline.py` + `ingestion/cli.py` — end-to-end orchestration
    (`make ingest`).
  - 82 unit tests + 4 integration tests (real yfinance calls).

### Changed
- Domain pivoted from sports betting to US equities (see ADR 0004).
- Data source pivoted from The Odds API to `yfinance` (no API key required).

### Fixed
- **Content hash instability due to vendor-derived `adj_close`.**
  `yfinance` recomputes `Adj Close` on every request, introducing float
  noise (~3e-5) between otherwise-identical fetches. This made the raw
  snapshot hash change on every run, silently breaking idempotency.
  Fixed by excluding `adj_close` from the content hash and rounding
  OHLCV prices to 4 decimal places. See
  `docs/adr/0006-content-hash-excludes-adjusted-close.md`.
  (Hash version bumped `raw_store:v2` → `raw_store:v3`.)
- **Makefile `ingest` target pointed at a library, not a CLI.**
  The target invoked `python -m ingestion.pipeline`, but that module is
  a library without a `__main__` guard; `make ingest` was a silent no-op.
  Now invokes `python -m ingestion.cli` and forwards `START`/`END`
  arguments. Caught by the full-universe integration run.
- **Unit tests constructed physically impossible OHLCV frames.**
  Adding the vectorized validator (`ingestion/validation.py`) made four
  existing tests fail because they modified `close` without adjusting
  `high`. Rewritten to modify `volume` instead — still a distinct
  snapshot, but a valid one.

### Added — M3.7 (fundamental ingestion)
- **`ingestion/sec/` package** for SEC EDGAR XBRL ingestion:
  - `config.py` — typed `SecSettings`, tag registry loader, skip list.
  - `schemas.py` — `SecFact`, `SecCompanyFacts`, `sec_facts_hash`.
  - `client.py` — `SecClient`. Ticker→CIK map (cached), companyfacts
    fetch, retry with exponential backoff, rate limit at 6.7 req/s.
    Two hosts: static files at www.sec.gov, XBRL API at data.sec.gov.
  - `raw_store.py` — immutable Parquet snapshots at
    data/raw/fundamentals/sec/<ticker>/CIK####__<hash16>.parquet.
  - `pipeline.py` + `cli.py` — orchestration, `dbp-ingest-sec`.
- **`config/fundamental_tags.yml`** — 137 unique tags in 6 categories
  (income statement, balance sheet, cash flow, per share, employees,
  macro-correlated).
- **`config/sec_skip_tickers.yml`** — entities that are not SEC
  reporting companies (SPY and future ETFs).
- **`make ingest-sec`** target, `dbp-ingest-sec` entry point.
- **Quality gate** extended with `SecFactsSchema` and `load_sec_facts`;
  layer name `"sec"` added to `--skip` choices.
- **45 new unit tests** for SEC modules.

### Fixed (M3.7)
- **SPY 404 treated as failure.** SPY is an ETF and does not file
  companyfacts. Skipped via `sec_skip_tickers.yml`, not failed.
- **SecFactsSchema too strict.** `filed >= period_end` was asserted
  at the raw layer, but SEC legitimately contains preliminary and
  forward-looking facts filed before the period ends. Removed from
  the raw schema; the PIT rule (`filed <= trade_date`) will be
  enforced in `int_fundamentals_pit` (M3.8).

### Known limitation (M3.7)
- XOM's ticker resolves to a 2024 reorganization entity
  (CIK 2115436) with limited history. Union of pre- and post-reorg
  CIKs is a future improvement.

### Added — M3.6 (macro warehouse)
- **dbt staging**: `stg_macro_series` with PIT-effective vintage_date
  for latest-mode series (ADR 0009).
- **dbt intermediate**:
  - `int_macro_vintages` — one row per (series_id, vintage_date);
    collapses 24M stg rows to ~280K.
  - `int_macro_daily` — PIT join (asof) to trade_dates. Materialized
    as TABLE for performance: 67s total, down from 1160s naive.
- **dbt marts**: `fct_macro_daily` — wide format, 140 macro columns
  + trade_date. Var-driven column list from `macro_series_ids`.
- **Singular tests**:
  - `assert_vintage_consistency` — no duplicate (series, vintage, obs).
  - `assert_macro_pit` — no look-ahead: vintage/observation <= trade_date.
  - `assert_macro_columns_count` — column count matches registry.
- **Utility scripts**:
  - `scripts/cleanup_macro_raw.py` — dedupe old reconstruction snapshots.
  - `scripts/cleanup_prices_raw.py` — keep widest snapshot per ticker.
  - `scripts/sync_macro_var.py` — sync dbt vars with YAML registry.

### Fixed (M3.6)
- **Latest-mode series were invisible historically.** FRED returns one
  vintage_date per series for daily data (e.g. 2026-09-30 for DGS10).
  Naive PIT join excluded them from all prior trade dates. Fixed by
  setting `vintage_date := observation_date` for these 28 series in
  staging. See ADR 0009.
- **Prices staging duplicated after extended ingest.** Append-only raw
  layer kept both narrow and extended snapshots; glob union produced
  duplicates. Cleanup script keeps the widest snapshot per ticker.

### Added — M3.5 (macro ingestion)
- **`ingestion/macro/` package** for vintage-aware macro ingestion:
  - `config.py` — typed `MacroSettings`, `MacroSeries` registry loader.
  - `schemas.py` — `MacroObservation`, `MacroSnapshot`, `macro_snapshot_hash`.
  - `client.py` — `FredClient` (FRED + ALFRED). One request per series
    using the `realtime_start=1776-07-04` sentinel; reconstructs
    cumulative vintages by interval join.
  - `raw_store.py` — immutable Parquet snapshots at
    `data/raw/macro/fred/<SERIES_ID>/<vintage>__<hash16>.parquet`
    with a JSON manifest.
  - `pipeline.py` + `cli.py` — end-to-end orchestration, `dbp-ingest-macro`.
- **`config/macro_series.yml`** — 149 curated FRED series across 10
  categories (monetary_policy, rates, inflation, employment, growth,
  money_credit, housing, energy, sentiment, international).
- **`make install-all`**, `make ingest-macro` targets.
- **47 new unit tests** (schemas, client, raw_store, pipeline, CLI).
  All mock-based; no network in CI.

### Added — M3 (data quality gate)
- **`quality/` package** with Pandera schemas at each layer boundary:
  - `RawPricesSchema` — raw Parquet contracts (ticker, OHLCV).
  - `StgPricesSchema` — typed staging contract.
  - `FctReturnsSchema` — marts contract (features + labels + dims).
- **`quality/gate.py`** — runner that loads each layer, validates,
  and produces a JSON-serialisable `GateReport`.
- **`quality/cli.py`** — `dbp-quality` entry point and `python -m
  quality.cli`. Flags: `--tickers`, `--tickers-from-raw`, `--skip`,
  `--json`, `--quiet`.
- **`scripts/demo_bad_data.py`** — 4-step story demonstrating that the
  gate catches OHLC violations and label NULL mismatches.
- **36 unit tests** for schemas/gate/cli, **6 integration tests** that
  corrupt a copy of the warehouse and assert the gate fails loudly.
- **CI**: quality step added to the `dbt-build` job; uses
  `--tickers-from-raw` so the same gate validates both the full
  universe (local) and the committed fixture (CI).
- **`make quality`, `make quality-json`, `make ci`** targets.
- **ADR 0008** — three-layer defense rationale.

### Added — M2 (dbt warehouse)
- **dbt project skeleton** (`dbt_project.yml`, `profiles.example.yml`,
  `packages.yml`).
- **Staging**: `stg_prices` (typed, renamed; view).
- **Intermediate**: `int_returns` (log returns + forward labels, point-in-time;
  view).
- **Marts**: `dim_tickers` (point-in-time membership), `fct_prices_daily`,
  `fct_returns_daily` (model-ready: features + labels).
- **Seed**: `ticker_metadata` (32 tickers, sector, benchmark flag).
- **Source freshness macro** (`raw_prices_freshness`).
- **Singular tests**: OHLC invariants, PIT return boundaries, return ranges,
  label NULL equivalence, benchmark presence.
- **Custom schema naming macro** (`generate_schema_name`) so schemas are
  `staging` / `intermediate` / `marts` rather than `main_staging` / etc.
- **CI job `dbt-build`** running against a committed fixture
  (`tests/fixtures/raw/prices/yfinance/`, 3 tickers x 21 days) — fully hermetic.
- **`scripts/make_test_fixture.py`** to regenerate the fixture from the raw layer.
- **`make dbt-build FIXTURE=1`** runs the fixture-mode build locally.

### Added (post-M1 hardening)
- `ingestion/validation.py` — vectorized OHLCV validator invoked on
  every `RawStore.write_snapshot`. Hard-fails on empty input, missing
  columns, bad index (name/order/duplicates), NaN, non-positive prices,
  OHLC invariant violations, and negative volume.
- ADR 0007 documents the two-layer validation design (Pydantic for
  rows at edges, vectorized for frames in the hot path).
- Full-universe smoke test verified: 32 tickers, idempotent on rerun.
