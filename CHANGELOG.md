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
