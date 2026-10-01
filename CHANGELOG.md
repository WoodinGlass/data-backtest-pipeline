# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial repository skeleton: folder structure, tooling, CI stub.
- Foundational files: `.gitignore`, `LICENSE` (MIT), `pyproject.toml`, `.env.example`.
- Architecture Decision Records (ADR 0001-0005).
- Data dictionary and runbook stubs.
- `ingestion/schemas.py` with `RawPriceEvent` contract + idempotency helpers.
- Unit tests for schema contract and payload hashing.

### Changed
- Domain pivoted from sports betting to US equities (see ADR 0004).
- Data source pivoted from The Odds API to `yfinance` (no API key required).
