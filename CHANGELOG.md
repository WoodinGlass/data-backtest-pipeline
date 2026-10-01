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

### Added (post-M1 hardening)
- `ingestion/validation.py` — vectorized OHLCV validator invoked on
  every `RawStore.write_snapshot`. Hard-fails on empty input, missing
  columns, bad index (name/order/duplicates), NaN, non-positive prices,
  OHLC invariant violations, and negative volume.
- ADR 0007 documents the two-layer validation design (Pydantic for
  rows at edges, vectorized for frames in the hot path).
- Full-universe smoke test verified: 32 tickers, idempotent on rerun.
